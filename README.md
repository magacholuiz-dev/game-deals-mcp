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

### Como o resolvedor da Nintendo acha o NSUID brasileiro

1. Página do produto em `nintendo.com/pt-br/store/products/<slug>/`: o NSUID
   está na URL da imagem do JSON-LD. O preço do JSON-LD é o **atual** (em
   promoção, o promocional), então o preço de verdade vem da API de preço, que
   também dá o preço cheio e a data de fim da promoção.
2. Se o slug derivado não existe, o **sitemap** `pt-br/store/sitemap.xml`
   (28 mil produtos) tem o slug real (`no-mans-sky-nintendo-switch-2-edition-switch-2`).
3. Validação: o nome da página precisa conter as âncoras do título, a versão
   (nativo, Edition, Switch 1) precisa bater, e o NSUID precisa **precificar em
   BR**. NSUID europeu devolve `not_found` no Brasil, e é isso que o rejeita.

Título fora do sitemap simplesmente não é vendido no Brasil (o *007 First Light*
não estava lá), não é erro de slug.

### PlayStation Store: edições, e por que não há descoberta

Cada página de produto embute um cache com **todas** as edições, cada uma com
preço, preço cheio e fim da promoção. O JSON-LD só descreve uma delas, que era o
erro da versão anterior. Pedir uma edição que não existe devolve **nada** (e diz
quais existem), e um conceito com mais de uma edição exige `id#edicao`
(`10000730#ultimate`) ou `product/<id>`: um id ambíguo é recusado em vez de
gravar o preço da Standard no produto Ultimate.

Descoberta por categoria **não é possível**: as páginas de categoria e de ofertas
devolvem a mesma casca renderizada no navegador, sem produto nenhum (conferido em
três categorias), e não há sitemap. Os dados vêm de GraphQL com hash que rotaciona.

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

## Histórico do varejo: o que deixou de existir

Uma versão anterior deste projeto guardava as ofertas **encerradas** do Promobit
(com preço, loja e data) e as mostrava como "já esteve por". Isso vinha de
`api.promobit.com.br`, cujo `robots.txt` proíbe qualquer acesso automatizado
(`Disallow: /` para todos os agentes). O projeto se compromete a obedecer o
`robots.txt`, então essa integração foi removida e o cliente HTTP recusa o host.

Sem ela não há mais fonte gratuita de histórico do varejo brasileiro. O que
existe hoje é o histórico que **o próprio coletor acumula** das lojas com API
(eShop, PS Store, Steam). Linhas antigas de `deal_signals` marcadas como
encerradas continuam legíveis, mas nenhuma nova é criada.

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

## Promobit: só a listagem pública

O que sobrou, e por quê. Medido em 21/09/2026:

- `api.promobit.com.br`: **proibido** pelo `robots.txt` (ver acima). Não é usado.
- `www.promobit.com.br/promocoes/games/`: permitido. É renderizada no servidor e
  traz as ofertas atuais em `__NEXT_DATA__`.
- Limites reais: cerca de 12 ofertas por página, quase todas hardware e cartões;
  `?page=` é ignorado pelo servidor; as subcategorias por plataforma dão 404; a
  busca (`/buscar*`) é proibida.

Ou seja, o feed só percebe uma oferta que por acaso esteja na primeira página de
games. Serve como pista, não como cobertura. Para cobertura confiável o caminho
são os feeds de afiliado (Lomadee, Awin) ou uma fonte de loja.

Como o site não pode ser buscado, o casamento é feito **localmente**, em
`matching.py`, em três camadas testadas com títulos reais:

1. **Âncoras**: tokens distintivos do título pedido precisam aparecer; numerais
   comparam como número ("VI" casa com "6", nunca com "V").
2. **Marcadores negativos**: acessório, cartão presente, moeda de jogo, DLC,
   console e bundle. Casam por palavra inteira e são ignorados quando fazem
   parte do título pedido, senão o jogo *Grip* jamais casaria com nada. Pedir
   "Hades" não aceita "Hades II".
3. **Banda de preço**: com preço de loja de referência, uma fração dele; sem
   referência, a mediana dos casados, com corte simétrico. Menos de três ofertas
   não sustentam mediana, então nada é cortado.

Continua sendo um *feed*: o que vem dele vai para `deal_signals`, nunca para
`price_points`, porque um post de usuário não é uma leitura periódica da loja.

### Pelando

`api-web.pelando.com.br` responde, mas todos os caminhos REST testados devolvem
404, provavelmente GraphQL por POST. Antes de implementar, ler o `robots.txt` do
host: o precedente do Promobit mostra que um host de API pode estar fechado.

## Rede, conformidade e fixtures

**Toda a rede passa por `game_deals/http.py`**: no máximo 1 requisição por
segundo por host, `robots.txt` lido e obedecido (interpretador próprio da RFC
9309 em `robots.py`, porque o `urllib.robotparser` ignora curingas como
`/buscar*`), cache condicional com `ETag` e `Last-Modified`, e um `User-Agent`
que se identifica. Verifiquei que todos os hosts servem o mesmo conteúdo para
esse agente, então não há disfarce nenhum. Um bloqueio vira `RobotsBlocked`, e
não é contornado.

**Os testes não usam a rede.** Um guard em `tests/conftest.py` derruba qualquer
teste que tente. As respostas vêm de `tests/fixtures/`, gravadas ao vivo por
`scripts/probe.py`, e `MANIFEST.json` guarda URL, data e sha256 de cada uma. Se
alguém editar uma fixture à mão, `tests/test_manifest.py` falha.

```bash
uv run python scripts/probe.py nintendo --check    # os parsers de verdade leem a resposta ao vivo de hoje
uv run python scripts/probe.py nintendo --record   # regrava as fixtures (recusa se o parser não lê)
```

Duas coisas que só apareceram porque as fixtures são reais: a normalização Unicode
transformava `™` em "TM" ("Mario Kart™ World" virava "karttm"), e o apóstrofo
separava "Man's" em duas palavras.

## Fontes

| Fonte | Estado | O que precisa |
|---|---|---|
| **Promobit** | ⚠️ parcial, sem chave | só a listagem pública de games (~12 ofertas); a API é proibida por robots.txt |
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

O agendador é um serviço com estado no próprio banco (`job_runs`, `job_state`),
não um horário fixo do `launchd`. O `launchd` só o mantém vivo.

```bash
scripts/install.sh --print     # mostra o plist gerado para ESTE checkout, sem instalar
scripts/install.sh             # instala e carrega
uv run game-deals-scheduler --status
```

O plist antigo tinha o caminho da casa do autor escrito dentro. Este é gerado a
partir do caminho do repositório e do usuário atual.

## Agendamento, lacunas e saúde das fontes

**Ciclos (ticks).** Cada trabalho tem uma cadência e o tempo é cortado em ciclos
dessa duração, deslocados por um jitter fixo por trabalho. Um trabalho está
devido quando não existe execução para o ciclo atual. Depois que o Mac dorme, os
ciclos perdidos **não são reexecutados**: uma execução de recuperação responde ao
último ciclo, e os perdidos ficam registrados como a diferença entre execuções
esperadas e feitas. É essa diferença que rebaixa a confiança do veredito.

| trabalho | cadência |
|---|---|
| eShop, PS Store | 12 h |
| Steam | 6 h |
| feed da comunidade | 1 h |
| ITAD | 1 dia |
| notas (RAWG) | 7 dias |
| backup | 1 dia |

Metade da cadência de 3 dias antes de uma promoção grande até ela acabar. Eventos
só *estimados* (Semana do Consumidor 2027) nunca aceleram a coleta.

**Falhas.** Uma execução que falha é repetida com recuo exponencial. Três falhas
seguidas abrem um **disjuntor** que pula o trabalho por um tempo crescente (até
6 h), para não martelar um site quebrado. Uma execução boa fecha o disjuntor.

**O que conta como falha da fonte.** `robots`, `timeout`, `http`, `drift` (o
parser não leu a resposta) e `other`. Um jogo que não existe na loja, ou uma
edição ambígua, é fato sobre o produto e **não** conta, senão uma fonte
saudável pareceria quebrada por causa de um título não vendido no Brasil.

### Confiança que enxerga lacunas

Antes, a confiança do veredito dependia só de quantas leituras existiam. Agora
ela também olha o ritmo de coleta no período que a frase alega: "menor preço em
7 meses" é julgado sobre 7 meses de execuções, não sobre a última semana.

- esperadas = tempo coberto ÷ cadência **da própria fonte**;
- feitas = janelas de tempo distintas com uma execução boa (falha não conta,
  parcial conta, duas execuções na mesma janela contam uma vez);
- lacuna pequena tira um nível de confiança, lacuna grande tira dois, nunca
  abaixo de "baixa"; sem selo e sem alerta de "menor preço" com confiança baixa.

Duas regras que evitam injustiça: uma coleta **semanal** que rodou toda semana é
perfeita, não "6 de 7 faltando"; e histórico que **antecede o agendador** ou foi
importado de fora (backfill do ITAD) não tem execuções para comparar, então não é
premiado nem punido. O veredito devolve o motivo: `confianca_motivos`,
`confianca_antes_das_lacunas` e `lacunas_de_coleta`.

### Saúde das fontes

`health.check_source_health()`, também em `GET /api/health` e na tool
`source_health` do MCP, classifica cada fonte só pelas execuções registradas:

- **quebrada**: disjuntor aberto, 3 falhas seguidas, 2 execuções seguidas com
  formato ilegível, `robots.txt` passou a proibir, a fonte que trazia itens não
  traz nada há 3 execuções, ou não há coleta boa há 4 cadências;
- **degradada**: última execução parcial, mais de 20% das recentes falharam,
  latência mediana alta, ou coleta um pouco atrasada;
- **saudável**, **sem dados** e **inativa** (sem credencial).

Você é avisado uma vez quando uma fonte quebra e uma vez quando ela sai de
"quebrada".

## Inteligência de preço

Tudo em `intel.py`, tudo regra que dá para ler e testar, sem modelo opaco. Cada
decisão vem com o motivo e com uma **incerteza**, e quando os dados não sustentam
uma afirmação a resposta é "neutro", nunca um palpite confiante. Dinheiro é sempre
centavo inteiro.

**Veredito agregado** (`veredito_agregado`): o menor preço **atual** entre as lojas
oficiais, comparado com todo o histórico de todas as lojas ("menor preço registrado
em todas as lojas em 7 meses"). Duas perguntas separadas de propósito: o que conta
como *disponível agora* passa pelo filtro de frescor (mais velho que 3 ciclos da
própria fonte, mínimo 3 dias, não é oferta ativa: um anúncio de 6 meses de uma loja
abandonada não pode aparecer como promoção), mas o que conta como *histórico* não
passa: um preço real de um ano atrás continua sendo real.

**Comprar ou esperar** (`recomendar_compra`), na ordem:

1. jogo **não lançado**: neutro (pré-venda não entra em promoção);
2. sem preço recente: neutro, incerteza máxima;
3. **promoção grande a até 30 dias** (Black Friday, Steam Winter…) e desconto
   **raso**: espere. Raso é menos da metade do desconto típico, ou menos de 15%
   quando não há base típica;
4. **menor preço** já registrado, com histórico: compre;
5. desconto **igual ou acima do típico** do publisher (ou da plataforma): compre;
6. o preço **já esteve menor** há pouco: espere;
7. senão, neutro.

A regra 3 vem antes da 4 de propósito: um mínimo alcançado com desconto raso, dias
antes de uma promoção grande, é um mínimo fraco. Eventos com data só *estimada*
aumentam a incerteza; a Steam Autumn (pequena) e promoções de outra plataforma não
disparam "espere". O desconto típico usa a **maior queda de cada produto** (uma
promoção longa conta uma vez, não uma por leitura) e **exclui o próprio produto**
da referência, que senão seria circular.

**Nota de oportunidade** (`pontuar_oportunidade`, 0 a 100): desconto real (peso
0,35), nota da crítica (0,25), valor por hora de jogo (0,20), popularidade em escala
log (0,10) e prioridade na wishlist (0,10). O desconto é medido contra o **preço
típico** que o comprador realmente viu (mediana do menor preço diário em um ano),
não contra o preço de tabela, que a loja pode inflar. R$ 5/h ou menos é nota máxima,
R$ 20/h ou mais, zero. Entrada ausente sai da conta e `cobertura` diz quanto do peso
tem dados.

**Orçamento** (`planejar_orcamento`): mochila 0/1 **exata** sobre centavos inteiros,
por programação dinâmica com poda de Pareto. Não é o guloso, que erra: o teste tem um
caso em que o melhor por real bloqueia dois itens que juntos valem mais. Verificado
contra força bruta em 300 instâncias aleatórias. A conversão do valor usa `Decimal`:
`int(4.35 * 100)` dá 434, e 137 dos primeiros 1.999 valores em centavos perdem um
centavo desse jeito. Por padrão segura o que a recomendação manda esperar e lista o
motivo.

**Comparações**: `comprar_edicoes` (Standard x Deluxe x Ultimate; o conteúdo bônus
**não** é avaliado, só preço e histórico), `comparar_upgrade_switch2` (jogo de
Switch 1 + Upgrade Pack contra a edição completa) e `comparar_midia` (física, que
vem de ofertas de usuários, recentes e sem frete, contra digital).

Um defeito que isso revelou e que já existia: um jogo cujo preço **nunca mudou**
era chamado de "menor preço já registrado", com selo dourado e alerta, porque
ninguém jamais foi mais barato. Preço estável não é oferta.

## Banco: backup, exportação e Docker

O histórico acumulado é o ativo mais valioso do projeto e não dá para recuperá-lo
de nenhuma API. O trabalho `backup` (diário) usa a API de backup do SQLite, que é
consistente mesmo com o coletor escrevendo, e **só guarda a cópia se ela passar
`integrity_check` e tiver pelo menos as mesmas linhas da origem**. Mantém as 14
mais novas e, se a verificação falhar, não apaga nenhuma cópia antiga.

```bash
uv run game-deals-backup                 # cópia verificada + rotação
uv run game-deals-backup list
uv run game-deals-backup export x.json   # portátil entre máquinas
uv run game-deals-backup import x.json   # não duplica: linhas presentes ficam
uv run game-deals-backup restore deals-20260921-030000.db --force
```

`restore` recusa sobrescrever sem `--force` e, mesmo com ele, guarda uma cópia de
segurança (`.before-restore`). Backup corrompido é rejeitado antes de tocar em
qualquer coisa.

Para rodar num servidor sempre ligado:

```bash
docker compose up -d --build
docker compose exec gamedeals game-deals-scheduler --status
```

Agendador e painel rodam **no mesmo contêiner**, com um volume de dados: o modo
WAL do SQLite precisa de memória compartilhada, o que não é confiável entre
contêineres num Mac. A porta é publicada só em `127.0.0.1`, porque o painel não
tem login. A imagem roda sem privilégios de administrador e não contém `.env`
nem testes; as chaves entram em tempo de execução. Testado de verdade: a imagem
constrói, o contêiner fica saudável, o agendador executa sozinho, um backup
verificado é gerado, e o histórico sobrevive a um reinício sem repetir trabalhos.

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
