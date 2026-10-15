# 🏠 Monitor de casas em Vila Nova de Gaia

Procura de 15 em 15 minutos em:

- **Portais:** Imovirtual, Supercasa, Casa Sapo, Trovit (agregador)
- **Classificados:** OLX, CustoJusto
- **Imobiliárias:** RE/MAX, ERA, Century 21, KW Portugal, Zome, Decisões e Soluções, iad, Engel & Völkers

e envia uma notificação **ntfy** sempre que aparece um anúncio novo:

- **T3 a T5**
- **500.000 € – 650.000 €**
- **Vila Nova de Gaia** (todas as freguesias)

Corre de graça no GitHub Actions — não precisas de ter o computador ligado.

## Instalação (5 minutos)

1. **Cria um repositório novo no GitHub** (público — ver nota abaixo) e carrega todos os ficheiros desta pasta, incluindo a pasta `.github`.
   - Mais fácil: no repositório vazio, *Add file → Upload files* e arrasta o conteúdo da pasta.
   - ⚠️ A pasta `.github` começa por ponto e às vezes fica escondida. Se não aparecer, cria à mão o ficheiro `.github/workflows/monitor.yml` (*Add file → Create new file*, escreve esse caminho no nome) e cola lá o conteúdo.
2. **Tópico ntfy**: *Settings → Secrets and variables → Actions → New repository secret*
   - Nome: `NTFY_TOPIC`
   - Valor: o código do tópico (ex.: o mesmo que já usas, ou um novo tipo `casas-gaia-x7k29q`). Na app ntfy, subscreve esse tópico.
3. **Permissões**: *Settings → Actions → General → Workflow permissions* → escolher **Read and write permissions** → Save.
4. **Testar**: separador *Actions* → *Monitor casas Gaia* → **Run workflow**.
   Em ~1 minuto recebes no telemóvel um resumo com o estado de cada site (✅ / ❌).

A partir daí corre sozinho. Na primeira passagem regista os anúncios que já existem (sem te encher de notificações); só avisa dos que aparecem depois.

## Como funciona

- `config.yaml` — critérios (preço, tipologia) e os URLs de pesquisa de cada site. Para mudar algo, edita este ficheiro diretamente no GitHub.
- `monitor.py` — lê as páginas, filtra por preço e tipologia, e notifica os anúncios novos.
- `seen.json` — anúncios já vistos (é atualizado automaticamente).

Cada notificação diz a tipologia, o preço e o site; tocar nela abre o anúncio.

## Se um site deixar de funcionar

Os sites mudam e alguns bloqueiam acessos automáticos. Os endereços de pesquisa das imobiliárias foram escritos sem os poder testar, por isso é normal alguns darem ❌ (ex.: `HTTP 404`) no primeiro teste — basta corrigir o URL como explicado abaixo. Se um site falhar durante ~2 horas seguidas, recebes **um aviso por dia** (⚠️) — os outros continuam a funcionar normalmente.

Para corrigir um URL: abre o site no browser, faz a pesquisa com os filtros (Gaia, preço, T3–T5, ordenar por mais recentes), copia o endereço e cola-o em `config.yaml` no lugar do antigo. Para desligar um site, apaga o bloco dele. Para acrescentar outra imobiliária, copia um bloco das imobiliárias e muda o `nome` e o `urls` — a deteção dos anúncios é automática.

Os sites marcados com `browser: true` são abertos num Chrome real (precisam de JavaScript); por isso cada execução demora 3–6 minutos.

## Notas

- **Repositório público**: o GitHub Actions é ilimitado em repositórios públicos. Num privado, correr de 15 em 15 minutos gasta ~2.900 minutos/mês, acima dos 2.000 grátis. O tópico ntfy fica guardado nos *Secrets* e não é visível; o repositório só tem a configuração e os IDs dos anúncios.
- O GitHub pode atrasar as execuções agendadas alguns minutos nas horas de maior carga.
- Para pausar: *Actions → Monitor casas Gaia → ⋯ → Disable workflow*.
