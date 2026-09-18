# game-deals-mcp

MCP para rastrear preço de jogos e hardware no Brasil, com **veredito histórico**:
não "está R$ 199", e sim *"menor preço dos últimos 1 ano e 2 meses"*.

## A ideia

O MCP é a casca. O valor está no **coletor + série temporal**. Se as tools saírem
buscando preço na hora e devolverem, você nunca consegue dizer "melhor preço em X
tempo" — isso exige histórico que alguém está gravando todo dia, independente de
você estar conversando com o Claude.

```
[coletor]  launchd 2x/dia → snapshot de preços → SQLite
[core]     verdict.py (veredito) + alerts.py (regras)  ← mesma lógica nos dois lados
[MCP]      server.py: tools finas que leem o core
```

### O cálculo

Em vez de perguntar "é o menor preço dos últimos 90 dias?" (90 é arbitrário),
invertemos: **quanto tempo preciso voltar para achar um preço menor?** Uma consulta
só, e a resposta já sai na linguagem que você queria.

Com um porém que custou um bug: essa frase é tecnicamente verdadeira mesmo quando o
preço **subiu** e a leitura de hoje é a única da janela. Por isso o veredito só usa
"menor preço dos últimos N" quando o preço atual realmente bate o mínimo recente;
caso contrário ele diz `"não é promoção — esteve a R$ 264,00 há 40 dias"`.

## Switch 2 e PS5

Três fontes, cada uma pelo que faz melhor — todas verificadas, nenhuma exige chave:

| para | fonte | por quê |
|---|---|---|
| descobrir e classificar Switch 2 | catálogo europeu (Solr) | é **o único** que conhece o Switch 2: `system_type:nintendoswitch2`, 552 títulos |
| preço em BRL | `api.ec.nintendo.com` + NSUID das Américas | cota em real |
| NSUID brasileiro de Switch 2 | página pt-br do produto | o índice pt-br é de uma geração antiga e **não tem Switch 2** |
| PS5 | JSON-LD da PS Store | schema.org, publicado para leitura automática |

### A armadilha que custa caro

**NSUID europeu não precifica no Brasil.** São espaços de id por região, e o erro
é silencioso — devolve `not_found`, não um erro:

```
70010000096802 (Mario Kart World, catálogo EU) → BR: not_found | GB: £66.99
70010000095431 (mesmo jogo, Américas)          → BR: R$ 439,90
```

Por isso `search()` devolve o **slug da loja pt-br** como `source_id`, não o NSUID
europeu que o Solr entrega de graça. O `fetch()` aceita os dois formatos e resolve
o slug contra a loja brasileira.

### Como o jogo roda no Switch 2

| chip | significado |
|---|---|
| **Switch 2 (nativo)** | feito para o console novo |
| **Switch 2 Edition (upgrade)** | jogo de Switch 1 com pacote de melhoria pago |
| **Switch 1 — roda no Switch 2** | retrocompatível |

## Notas e popularidade

`discover_top(platform="switch2"\|"ps5", min_metacritic=80)` ordena por
`relevancia` = **60% nota + 40% popularidade em escala log**.

A escala log é o ponto: sem ela, um blockbuster com 19 mil jogadores achata todo o
resto e o ranking vira "o mais popular vence", ignorando a nota. Com ela, um 97
obscuro (83.0) empata com um 72 popularíssimo (83.2), e só o 97 popular (98.2)
fica no topo.

> **"Muita venda" não existe em API nenhuma.** Nem Nintendo nem Sony publicam
> vendas por título. O proxy honesto é quantas pessoas adicionaram o jogo à
> biblioteca — por isso o campo se chama `popularidade`, não `vendas`. Correlaciona
> bem para jogos grandes; não confunda com número de cópias vendidas.

Precisa da chave gratuita do RAWG (`RAWG_API_KEY`, só e-mail) — sem ela,
`discover_top` e `enrich_ratings` ficam inertes e o resto roda igual.

## Histórico do varejo sem credencial nenhuma

O Promobit devolve, junto das ofertas ativas, as **encerradas** — com preço, loja
e data. Elas não servem para comprar hoje, mas são preços reais observados, e são
o único histórico gratuito que existe para o varejo brasileiro.

O coletor só recorre a elas quando não há oferta ativa: sem isso o produto ficaria
sem preço nenhum. Entram marcadas como `active=0`, aparecem no card numa seção
própria — *"já esteve por"* — e **não disparam alerta**, porque oferta encerrada é
histórico, não notícia.

Foi o que resolveu a lacuna de Elden Ring e Baldur's Gate III: sem promoção ativa,
mas com preços passados de Shopee, Magazine Luiza e Nuuvem, datados.

## Mercado Livre (opcional)

Depois do parágrafo acima, o ML deixou de ser necessário — ele daria preço de
catálogo permanente, o que é melhor, mas não é mais a diferença entre ter e não
ter preço. Fica documentado para quando você quiser.

Desde 2024 o ML exige token em **tudo** — `/sites/MLB/search` sem `Authorization`
devolve 403, e até `/sites/MLB` devolve 403. Não existe caminho anônimo.

O fluxo documentado é **authorization_code** (não `client_credentials`), o que
significa aprovar o app com a sua conta uma vez. O `scripts/ml_auth.py` faz a
parte chata: sobe um servidor local, captura o `code` da volta, troca por tokens
e grava o `ML_REFRESH_TOKEN` no `.env`.

```bash
# 1. developers.mercadolivre.com.br/devcenter → Criar aplicação
# 2. URIs de redirect: http://localhost:8788/callback  (exatamente assim)
# 3. Cole Client ID e Secret no .env, então:
uv run python scripts/ml_auth.py
```

O provider tenta `refresh_token` primeiro e cai para `client_credentials` se você
tiver uma aplicação que aceite. **O ML rotaciona o refresh_token a cada uso**: a
renovação devolve um novo e mata o anterior, então gravamos o novo no `.env` a
cada renovação — sem isso a integração para sozinha depois de algumas horas.

### O que ele ainda acrescentaria

Preço de catálogo **permanente e diário**, em vez de só o que a comunidade postou.
Com ele os jogos de PS5 teriam série temporal própria, e o veredito "menor preço
em X tempo" valeria para eles como vale para os da eShop.

Duas armadilhas, se você voltar a isso: o ML **exige HTTPS** na URI de redirect
(`http://localhost` é recusado na validação, sem explicar por quê — o
`scripts/ml_auth.py` já sobe HTTPS local com certificado autoassinado), e o fluxo
é **authorization_code**, não `client_credentials`.

## Sites brasileiros: Promobit

O Promobit tem **API JSON pública** (`api.promobit.com.br/search`) e cobre de uma
vez as lojas que não têm API própria:

> KaBuM! · Netshoes · Magazine Luiza · Casas Bahia · Americanas · Amazon · Shopee

Integrar cada varejista separadamente daria muito mais trabalho e cobriria menos.

### Por que ele NÃO entra no histórico

Este provider tem `kind = "feed"`, e o coletor grava o que vem dele em
`deal_signals` — **nunca** em `price_points`. A razão é o veredito:

- os outros providers observam o preço de uma loja **periodicamente** — é uma série;
- o Promobit traz oferta **postada por gente** — aparece uma vez e some;
- o preço postado pode estar errado, expirado ou ser de vendedor duvidoso.

Misturar as duas coisas envenenaria o "menor preço em X tempo", que depende de
leituras regulares da mesma loja. No card eles aparecem numa seção separada,
*Ofertas da comunidade*, e disparam alerta — mas não movem o histórico.

### Dois filtros que o feed exige

**Busca progressiva.** A busca do Promobit é literal: `"Zelda: Breath of the Wild"`
devolve zero, `"zelda"` devolve dezenas. Consultamos do termo mais específico ao
mais largo e paramos no primeiro que responde. A âncora do filtro é o termo que
**casou**, não o mais largo — usar o mais largo deixava passar acessório.

**Banda de preço.** Título não separa acessório de jogo: uma capa de PS5 chamada
*"Faceplate GTA VI"* casa com a mesma busca do jogo. A banda (35%–125% do preço
atual de loja) corta o grosso — a capa de R$ 15,83 num jogo de R$ 349 cai fora — e
o card mostra o **título da oferta**, não só a loja, para você julgar o que sobrar.

Nenhum dos dois é perfeito. Um acessório caro perto do preço do jogo ainda passa;
por isso o card mostra o título e o rótulo é "sinal", não "preço".

### Pelando

`api-web.pelando.com.br` existe e responde, mas todos os caminhos REST testados
devolvem 404 — deve ser GraphQL por POST. Não implementado.

## Fontes

| Fonte | Estado | O que precisa |
|---|---|---|
| **Promobit** | ✅ funciona sem chave | — (agrega KaBuM!, Netshoes, Magalu, Casas Bahia, Amazon, Shopee) |
| **Steam BR** | ✅ funciona sem chave | — |
| **PlayStation Store BR** | ✅ funciona sem chave | — (JSON-LD da página) |
| **Nintendo eShop** | ✅ funciona sem chave | — (preço BR em BRL, com janela da promoção) |
| **IsThereAnyDeal** | chave grátis | `ITAD_API_KEY` — [pegue aqui](https://isthereanydeal.com/apps/my/) |
| **Mercado Livre** | app grátis | `ML_CLIENT_ID` / `ML_CLIENT_SECRET` no [DevCenter](https://developers.mercadolivre.com.br/devcenter) |
| **Amazon BR** | PA-API v5 | Associados aprovado **com vendas qualificadas** |
| **Shopee BR** | Affiliate Open API | cadastro em [affiliate.shopee.com.br](https://affiliate.shopee.com.br) |

Cada provider se auto-declara configurado ou não — o sistema roda com o subconjunto
que você tiver. `sources_status()` mostra o que falta.

**Sobre Amazon e Shopee:** não há raspagem aqui. Raspar as duas viola os termos e
quebra em dias contra a detecção de bot. Se precisar cobri-las antes de ter as
credenciais, o caminho é plugar um serviço SERP comercial (Rainforest, Oxylabs)
como um provider novo — a interface são as mesmas três funções em `providers/base.py`.

**ITAD é o atalho para o cold start:** ele já traz anos de histórico. Para jogos de
PC, `seed_history_from_itad()` no dia 1 e você nasce com veredito confiável. Para
console e hardware não existe atalho — você acumula, e o campo `confianca` diz
honestamente quando ainda não dá para confiar.

## Instalação

```bash
cd game-deals-mcp
uv sync
cp .env.example .env      # preencha o que tiver; nada é obrigatório
uv run pytest             # 15 testes
```

### Registrar no Claude Code

```bash
claude mcp add game-deals -- uv --directory /Users/luizfernandomagacho/dev/game-deals-mcp run game-deals-mcp
```

### Agendar a coleta

```bash
cp scripts/com.luiz.gamedeals.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.luiz.gamedeals.plist
```

Roda 9h30 e 21h30, grava em `collector.log`. Alertas saem por
[ntfy.sh](https://ntfy.sh) (push no celular, sem cadastro — escolha um tópico
difícil de adivinhar) e/ou notificação nativa do macOS.

## Dashboard

```bash
GAMEDEALS_DB=./demo.db uv run python scripts/seed_demo.py   # dados de exemplo
./scripts/demo.sh                                            # http://localhost:8787
```

Para o seu banco de verdade, sem o demo: `./scripts/web.sh`.

Cards com a arte do jogo, preço atual, sparkline de 12 meses e o **selo**, em três
níveis:

| selo | quando |
|---|---|
| 🟡 **MENOR PREÇO HISTÓRICO** | nada mais barato em todo o histórico coletado |
| 🟢 **DESTAQUE** | menor preço em ≥ 90 dias |
| 🔵 **BOM PREÇO** | menor preço em ≥ 30 dias |

O selo **nunca** aparece com histórico fraco (`confianca == "baixa"`) nem quando o
preço está acima do mínimo dos últimos 90 dias. A regra mora em
`verdict.highlight()`, não no front — assim o dashboard e o MCP nunca discordam
sobre o que é destaque. Os limiares são as duas constantes no topo daquela função.

Um detalhe que confundia no primeiro desenho: o mínimo do sparkline costuma ser
**menor** que o preço de hoje mesmo em card com selo — porque o selo olha 90 dias
e a linha, 12 meses. O rótulo mostra a data do mínimo (`mín. R$ 289,82 em 10/11/25`),
o que transforma a aparente contradição na explicação do selo.

As imagens vêm da própria fonte (Steam `header_image`, Nintendo, `pictures[0]` do ML,
`Images.Primary.Large` da Amazon, `imageUrl` da Shopee) e ficam gravadas no produto
na primeira coleta que trouxer uma.

> O botão *Atualizar preços* consulta as fontes em série — com ~6 produtos leva
> uns 7 segundos. A coleta de rotina é o `launchd`, não o botão.

## Tools

| tool | o que faz |
|---|---|
| `sources_status` | quais fontes estão ativas e o que falta nas outras |
| `search_sources` | busca em todas as fontes, devolve os `source_id` |
| `register_product` | cria o produto ligando os ids das várias fontes |
| `list_tracked` | catálogo, com nº de fontes e leituras |
| `get_price` | preço em todas as lojas + veredito histórico (grava snapshot) |
| `price_history` | série temporal |
| `seed_history_from_itad` | semeia anos de histórico (só PC) |
| `watch` / `list_watches` / `unwatch` | watchlist |
| `pending_alerts` | o que disparou desde a última checagem |
| `run_collection` | roda a coleta agora |

### Regras de alerta

`price_below` · `discount_above` · `new_low` · `any_drop` · `back_in_stock`

`back_in_stock` existe para o caso GTA VI: **título que ainda não lançou não entra
em desconto**. O que muda é estoque e edição especial esgotando, então a regra
precisa ser outra.

## A parte difícil não é a coleta

É a **identidade de produto**. "GTA VI PS5" na Amazon, no ML e na Shopee são três
strings diferentes. Por isso `aliases` é preenchido à mão em `register_product` —
casar por similaridade de título gera alerta no produto errado, e alerta errado
mata a confiança no sistema inteiro.

Fluxo: `search_sources("GTA VI")` → escolhe os candidatos certos →
`register_product` com os `source_id` de cada fonte → `watch`.
