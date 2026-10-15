#!/usr/bin/env python3
"""
Monitor de casas em Vila Nova de Gaia -> notificações ntfy.

Corre em GitHub Actions de 15 em 15 minutos. Guarda os anúncios já vistos
em seen.json (commitado de volta para o repositório).
"""
import json
import os
import re
import sys
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qsl

import yaml
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as http
    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:  # fallback
    import requests as http
    IMPERSONATE = {}

ROOT = Path(__file__).parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
STATE_FILE = ROOT / "seen.json"

F = CONFIG["filtros"]
NTFY_SERVER = os.environ.get("NTFY_SERVER") or CONFIG.get("ntfy_server", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
MANUAL = os.environ.get("MANUAL_RUN") == "true"
MAX_DETALHES = 15  # máx. de páginas de anúncio abertas por execução para descobrir a tipologia

# Deteção automática de links de anúncios (quando o site não tem "link" no config)
DEFAULT_LINK = re.compile(
    r"(?:imove(?:l|is)|propert|moradia|apartamento|anuncio|listing|detalhe|/casa[s]?/|/ref)"
    r"[^\"'#]*?(\d{4,}[\w-]*)", re.I)
IGNORAR_LINK = re.compile(r"(agente|consultor|agent|empreendimento|blog|noticia|contact|login|favorit)", re.I)

PRICE_RE = re.compile(r"(?<![\w.,])(\d{1,3}(?:[.\s  ]\d{3})+|\d{5,7})\s*(?:€|EUR)", re.I)
PRICE_RE2 = re.compile(r"€\s*(\d{1,3}(?:[.\s  ]\d{3})+|\d{5,7})(?![\d])")
ROOMS_RE = re.compile(r"\b[TV]\s?(\d{1,2})(?:\s?\+\s?\d)?\b")
QUARTOS_RE = re.compile(r"(\d{1,2})\s*(?:quartos|quarto|assoalhadas|bedrooms?)", re.I)


# ---------------------------------------------------------------- utilidades
def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def parse_price(text, menor=False):
    """Primeiro preço do texto; com menor=True, o mais baixo (cartões com preço riscado após descida)."""
    vals = []
    for rx in (PRICE_RE, PRICE_RE2):
        for m in rx.finditer(text):
            try:
                v = int(re.sub(r"\D", "", m.group(1)))
            except ValueError:
                continue
            if 20000 <= v <= 20000000:  # ignora rendas, condomínio, €/m², etc.
                if not menor:
                    return v
                vals.append(v)
    return min(vals) if vals else None


def parse_rooms(text):
    m = ROOMS_RE.search(text)
    if m:
        return int(m.group(1))
    m = QUARTOS_RE.search(text)
    if m:
        return int(m.group(1))
    return None


def preco_ok(p):
    return p is not None and F["preco_min"] <= p <= F["preco_max"]


def tip_ok(r):
    return r is not None and F["tipologia_min"] <= r <= F["tipologia_max"]


def fmt_price(p):
    return f"{p:,}".replace(",", ".") + " €"


HEADERS = {
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def fetch(url):
    headers = dict(HEADERS)
    if not IMPERSONATE:
        headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    r = http.get(url, headers=headers, timeout=30, **IMPERSONATE)
    return r.status_code, r.text


# ---------------------------------------------------------------- browser (sites com JavaScript)
class Browser:
    def __init__(self):
        self.pw = self.browser = self.ctx = None

    def _start(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        try:  # o runner do GitHub já tem o Google Chrome instalado
            self.browser = self.pw.chromium.launch(channel="chrome", headless=True)
        except Exception:
            self.browser = self.pw.chromium.launch(headless=True)
        self.ctx = self.browser.new_context(
            locale="pt-PT", timezone_id="Europe/Lisbon",
            viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"))

    def get(self, url):
        if self.ctx is None:
            self._start()
        page = self.ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            status = resp.status if resp else 0
            # aceitar cookies, se aparecer o aviso
            for txt in ("Aceitar", "Aceitar todos", "Aceito", "Concordo", "Accept all", "Permitir todos"):
                try:
                    b = page.get_by_role("button", name=re.compile(f"^{txt}", re.I)).first
                    if b.is_visible(timeout=500):
                        b.click(timeout=1500)
                        break
                except Exception:
                    pass
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            for _ in range(4):  # faz scroll para carregar anúncios "lazy"
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(700)
            return status, page.content()
        finally:
            page.close()

    def close(self):
        try:
            if self.browser:
                self.browser.close()
            if self.pw:
                self.pw.stop()
        except Exception:
            pass


BROWSER = Browser()


# ---------------------------------------------------------------- parsers
def real_href(href):
    """Alguns sites (ex.: Casa Sapo) embrulham o link num contador: ...counter.aspx?l=<link real>."""
    if "?" in href:
        for k, v in parse_qsl(urlparse(href).query):
            if k.lower() in ("l", "url", "u", "redirect", "dest") and v.startswith("http"):
                return v
    return href


def parse_imovirtual_json(html, base):
    """O Imovirtual (Next.js) traz os resultados num JSON dentro da página."""
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None
    try:
        data = json.loads(tag.string)
        items = data["props"]["pageProps"]["data"]["searchAds"]["items"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    rooms_map = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5, "SIX": 6,
                 "SEVEN": 7, "EIGHT": 8, "NINE": 9, "TEN": 10, "MORE": 11}
    out = []
    for it in items or []:
        try:
            price = (it.get("totalPrice") or {}).get("value")
            rooms = it.get("roomsNumber")
            rooms = rooms_map.get(rooms) if isinstance(rooms, str) else rooms
            slug = it.get("slug") or ""
            loc = ""
            try:
                addr = it["location"]["address"]
                loc = ", ".join(x["name"] for x in (addr.get("street"), addr.get("city")) if x)
            except (KeyError, TypeError):
                pass
            out.append({
                "id": str(it.get("id")),
                "title": it.get("title") or "",
                "price": int(price) if price else None,
                "rooms": int(rooms) if isinstance(rooms, (int, float)) else parse_rooms(it.get("title") or ""),
                "url": f"https://www.imovirtual.com/pt/anuncio/{slug}" if slug else base,
                "where": loc,
            })
        except Exception as e:  # noqa
            log("  item ignorado:", e)
    return out


def parse_generic(html, base, link_re):
    """
    Funciona em qualquer site: procura os links de anúncios e, para cada um,
    sobe na árvore HTML até ao "cartão" que contém o preço desse anúncio.
    """
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base).netloc.replace("www.", "")
    pat = link_re or DEFAULT_LINK
    candidatos = {}
    for a in soup.find_all("a", href=True):
        href = real_href(a["href"].strip())
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = urljoin(base, href.split("#")[0])
        if link_re is None:
            # deteção automática: só links do próprio site e com ar de anúncio
            if host not in urlparse(full).netloc or IGNORAR_LINK.search(full):
                continue
            if full.rstrip("/") == base.rstrip("/"):
                continue
        m = pat.search(href) or pat.search(full)
        if not m:
            continue
        lid = next((g for g in m.groups() if g), None) or full
        candidatos.setdefault(lid, []).append((a, full))

    ids = set(candidatos)
    out = []
    for lid, anchors in candidatos.items():
        a, full = anchors[0]
        title = ""
        for x, _ in anchors:
            t = x.get("title") or x.get("aria-label") or x.get_text(" ", strip=True)
            if len(t) > len(title):
                title = t
        # sobe até ao cartão: o maior contentor que só tem este anúncio
        node, card_text = a, a.get_text(" ", strip=True)
        for _ in range(10):
            parent = node.parent
            if parent is None or parent.name in ("body", "html", "[document]"):
                break
            outros = 0
            for b in parent.find_all("a", href=True):
                bh = real_href(b["href"])
                mm = pat.search(bh) or pat.search(urljoin(base, bh))
                if mm:
                    other = next((g for g in mm.groups() if g), None) or urljoin(base, bh)
                    if other != lid and other in ids:
                        outros += 1
                        break
            if outros:
                break
            node = parent
            card_text = node.get_text(" ", strip=True)
            if parse_price(card_text) is not None and len(card_text) > 60:
                break
        if len(title) < 8 and node is not a:
            h = node.find(["h1", "h2", "h3", "h4", "h5", "h6"])
            title = h.get_text(" ", strip=True) if h else card_text[:100]
        out.append({
            "id": lid,
            "title": re.sub(r"\s+", " ", title)[:140],
            "price": parse_price(card_text, menor=True),
            "rooms": parse_rooms(title) or parse_rooms(full.replace("-", " ")) or parse_rooms(card_text),
            "url": full,
            "where": "",
        })
    return out


def detalhe(url, usar_browser):
    """Abre a página do anúncio para descobrir tipologia/preço que faltem."""
    try:
        status, html = BROWSER.get(url) if usar_browser else fetch(url)
        if status != 200:
            return None, None
        soup = BeautifulSoup(html, "html.parser")
        meta = " ".join(m.get("content", "") for m in soup.find_all("meta")
                        if m.get("property", "").startswith("og:") or m.get("name") == "description")
        head = (soup.title.get_text(" ", strip=True) if soup.title else "") + " " + meta
        body = soup.get_text(" ", strip=True)[:20000]
        return (parse_rooms(head) or parse_rooms(body)), (parse_price(head) or parse_price(body))
    except Exception as e:
        log("  detalhe falhou:", type(e).__name__)
        return None, None


def scrape_site(site, cfg):
    """Devolve (anúncios, erro_ou_None)."""
    link_re = re.compile(cfg["link"]) if cfg.get("link") else None
    usar_browser = bool(cfg.get("browser"))
    all_items, errors = {}, []
    for url in cfg.get("urls") or []:
        modos = [True] if usar_browser else [False, True]  # sem browser falhou? tenta com browser
        for com_browser in modos:
            erro, items = None, []
            try:
                status, html = BROWSER.get(url) if com_browser else fetch(url)
            except Exception as e:
                msg = re.search(r"net::\w+|Timeout", str(e))
                erro = msg.group(0) if msg else type(e).__name__
            else:
                if status not in (200, 0):
                    erro = f"HTTP {status}"
                else:
                    items = parse_imovirtual_json(html, url) if site == "imovirtual" else None
                    if items is None:
                        items = parse_generic(html, url, link_re)
                    # um cartão sem preço normalmente é um falso positivo (menu, rodapé…)
                    items = [it for it in items if it["price"] is not None]
                    if not items:
                        low = html.lower()
                        bloq = ("captcha", "datadome", "are you a robot", "cf-challenge", "just a moment")
                        erro = "bloqueado (captcha)" if any(k in low for k in bloq) else "0 anúncios lidos"
            log(f"  {'[browser] ' if com_browser else ''}{len(items)} anúncios em {url[:80]}"
                + (f" — {erro}" if erro else ""))
            if items:
                if com_browser:
                    usar_browser = True
                for it in items:
                    all_items[it["id"]] = it
                break
            errors.append(erro)
        time.sleep(random.uniform(1, 3))
    err = None if all_items else (", ".join(sorted(set(errors))) or "sem resultados")
    return list(all_items.values()), err, usar_browser


# ---------------------------------------------------------------- ntfy
def notify(title, message, click=None, tags=None, priority=3):
    if not NTFY_TOPIC:
        log("  [sem NTFY_TOPIC]", title, "|", message.replace("\n", " / "))
        return
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "tags": tags or [], "priority": priority}
    if click:
        payload["click"] = click
        payload["actions"] = [{"action": "view", "label": "Abrir anúncio", "url": click}]
    try:
        import requests
        r = requests.post(NTFY_SERVER.rstrip("/"), json=payload, timeout=20)
        log("  ntfy", r.status_code)
    except Exception as e:
        log("  ntfy falhou:", e)


# ---------------------------------------------------------------- main
def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    state.setdefault("seen", {})
    state.setdefault("health", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    resumo, novos_total, detalhes_feitos = [], 0, 0

    try:
        for site, cfg in (CONFIG.get("sites") or {}).items():
            if not cfg or not cfg.get("urls"):
                continue
            nome = cfg.get("nome", site)
            log(f"→ {nome}")
            try:
                items, err, usar_browser = scrape_site(site, cfg)
            except Exception as e:
                items, err, usar_browser = [], f"erro {type(e).__name__}", False
            health = state["health"].setdefault(site, {"falhas": 0, "ultimo_alerta": ""})

            if err:
                health["falhas"] += 1
                resumo.append(f"❌ {nome}: {err}")
                if health["falhas"] >= 8 and health["ultimo_alerta"] != today:
                    notify(f"⚠️ {nome} sem resultados",
                           f"Há {health['falhas']} tentativas seguidas que não consigo ler o {nome} ({err}). "
                           "Pode estar a bloquear o GitHub ou ter mudado o site. Os outros continuam ativos.",
                           tags=["warning"], priority=2)
                    health["ultimo_alerta"] = today
                continue

            health["falhas"] = 0
            seen = state["seen"].get(site)

            if seen is None:  # primeira vez neste site: regista sem notificar
                state["seen"][site] = {it["id"]: today for it in items}
                n_ok = sum(1 for it in items if preco_ok(it["price"]) and tip_ok(it["rooms"]))
                resumo.append(f"✅ {nome}: {len(items)} lidos ({n_ok} já dentro dos critérios)")
                continue

            resumo.append(f"✅ {nome}: {len(items)} lidos")
            for it in items:
                if it["id"] in seen:
                    continue
                seen[it["id"]] = today
                if not preco_ok(it["price"]):
                    continue
                if it["rooms"] is None and detalhes_feitos < MAX_DETALHES:
                    detalhes_feitos += 1
                    r, p = detalhe(it["url"], usar_browser)
                    it["rooms"] = r
                    if p:
                        it["price"] = p
                if not (preco_ok(it["price"]) and tip_ok(it["rooms"])):
                    if it["rooms"] is None and detalhes_feitos >= MAX_DETALHES:
                        del seen[it["id"]]  # tenta outra vez na próxima execução
                    continue
                novos_total += 1
                linhas = [it["title"]] if it["title"] else []
                if it["where"]:
                    linhas.append(f"📍 {it['where']}")
                notify(f"🏠 T{it['rooms']} · {fmt_price(it['price'])} · {nome}",
                       "\n".join(linhas) or "Novo anúncio",
                       click=it["url"], tags=["house"], priority=4)
                time.sleep(0.5)
    finally:
        BROWSER.close()

    # esquece IDs com mais de 120 dias para o ficheiro não crescer sem fim
    cutoff = time.time() - 120 * 86400
    for d in state["seen"].values():
        for k in [k for k, v in (d or {}).items()
                  if datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() < cutoff]:
            del d[k]

    if MANUAL or not state.get("arrancou"):
        notify("🔎 Monitor de casas em Gaia ativo" if not state.get("arrancou") else "🔎 Teste do monitor",
               f"T{F['tipologia_min']}–T{F['tipologia_max']}, {fmt_price(F['preco_min'])} – {fmt_price(F['preco_max'])}\n"
               + "\n".join(resumo), tags=["mag"], priority=2)
        state["arrancou"] = True

    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True))
    log(f"Concluído. Novos anúncios notificados: {novos_total}")
    log("\n" + "\n".join(resumo))


if __name__ == "__main__":
    sys.exit(main())
