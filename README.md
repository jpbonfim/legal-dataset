# dataset-juridico

Pipeline em Python que monta um dataset da **legislação federal brasileira em vigor**, coletada
direto das fontes oficiais, para *Continuous Pre-Training* (CPT) de modelos de linguagem
jurídicos. O resultado são arquivos Parquet com o texto vigente de cada norma, os metadados de
identificação e a contagem de tokens no tokenizer do modelo que será treinado.

## O dataset

### Cobertura

| Tipo de norma | Subset | Período consultado |
|---|---|---|
| Constituição Federal de 1988 | `leis` | 1988 |
| Emendas constitucionais | `leis` | 1992– |
| Leis complementares | `leis` | 1967– |
| Leis ordinárias (numeradas) | `leis` | 1889– |
| Decretos-lei | `leis` | 1937–1988 |
| Medidas provisórias | `leis` | 1988– |
| Decretos (numerados) | `decretos` | 1889– |

O período anterior a 1988 é coletado porque a legislação anterior à Constituição continua
valendo no que for compatível com ela. Código Penal, CLT, CTN e Lei das S.A. são exemplos.
Cada norma tem a vigência verificada antes do download. Os tipos e o intervalo de anos são
configuráveis.

### O que entra no texto

- **Só normas em vigor.** As revogadas são descartadas antes do download. Das medidas
  provisórias reeditadas, só a última edição é mantida.
- **Só dispositivos em vigor.** O texto riscado nas compilações do Planalto é removido, assim
  como os dispositivos que se resumem a "(Revogado)" ou "(VETADO)" e o bloco de assinaturas.
- **Sem anotações editoriais.** Notas como "(Redação dada pela Lei nº 13.874, de 2019)",
  "(Incluído pela…)" e "(Vide ADI…)" se repetem milhares de vezes e não fazem parte da norma.
  Elas saem do texto e ficam estruturadas na coluna `annotations`.
- **Um dispositivo por linha** (artigo, parágrafo, inciso, alínea), com uma linha em branco
  entre artigos. O título e a ementa abrem o texto.

```
LEI Nº 15.063, DE 23 DE DEZEMBRO DE 2024
Abre aos Orçamentos Fiscal e da Seguridade Social da União, em favor do Ministério Público da União, crédito suplementar […]
O PRESIDENTE DA REPÚBLICA Faço saber que o Congresso Nacional decreta e eu sanciono a seguinte Lei:

Art. 1º Fica aberto aos Orçamentos Fiscal e da Seguridade Social da União […]
```

### Filtros e deduplicação

Antes da exportação, cada documento passa por:

- **Qualidade.** Saem os documentos sem nenhum artigo (página vazia ou quebrada), os
  revogados por inteiro cujo aviso de revogação só aparece na própria página, e os textos com
  problemas de codificação. Um texto com pelo menos um artigo nunca é descartado por tamanho.
- **Deduplicação** exata (sha256) e aproximada (MinHash, similaridade ≥ 0,9). Entre cópias,
  fica a de texto mais atualizado e, em empate, a da norma mais antiga.

Cada documento descartado tem o motivo registrado. O relatório final mostra a contagem por
motivo: revogada, sem fonte de texto, filtro de qualidade ou duplicata.

### Formato

Os arquivos ficam em `data/final/leis/` e `data/final/decretos/`, divididos em partes de até
20 mil linhas e comprimidos com zstd.

| Coluna | Descrição |
|---|---|
| `id` | URN LexML, ex. `urn:lex:br:federal:lei:1990-09-11;8078` |
| `text` | Texto vigente, um dispositivo por linha |
| `title`, `ementa` | Título e ementa |
| `tipo`, `numero`, `data_assinatura` | Identificação da norma |
| `esfera`, `uf`, `municipio` | Jurisdição (hoje sempre `federal`) |
| `fonte`, `url` | Fonte e endereço do texto baixado |
| `versao_texto` | `current`: compilação só com o que está em vigor; `compiled`: compilação com o revogado riscado, já removido; `original`: texto como publicado |
| `annotations` | Anotações removidas do texto: `{tipo, texto, norma_ref}` |
| `n_chars`, `n_palavras`, `n_tokens` | Tamanhos, com tokens contados no tokenizer configurado |
| `sha256`, `data_coleta` | Hash do texto e data do download |

O pipeline também gera `data/final/stats.json`, com documentos e tokens por subset, tipo,
fonte e década, e `data/reports/source_map.json`, com a cobertura do catálogo e a vigência e
fonte do texto por tipo de norma.

### Limitações

- **A vigência é a formal.** Uma norma antiga que nunca foi revogada expressamente entra no
  dataset mesmo que tenha perdido o efeito na prática. Isso pesa principalmente nos decretos.
- **`versao_texto = original`** é o texto como publicado. Quando o Planalto não tem compilação
  de uma norma, alterações posteriores não estão refletidas no texto.
- **A informação de vigência pode estar atrasada** em relação ao Planalto. Uma norma revogada
  recentemente ainda pode aparecer como vigente. Nesse caso, a página do Planalto já traz o
  texto riscado, e a norma sai pelo filtro de qualidade.

## Fontes

| Fonte | Uso |
|---|---|
| [Dados Abertos do Senado](https://legis.senado.leg.br/dadosabertos) | Catálogo das normas por tipo e ano; vigência quando o normas.leg.br não a informa |
| [normas.leg.br](https://normas.leg.br) (Congresso Nacional) | Vigência e texto compilado vigente (CC BY 4.0) |
| [Planalto](https://www.planalto.gov.br/ccivil_03) | Texto das normas sem compilação no normas.leg.br, a maior parte do volume |

Textos de leis e atos oficiais não são protegidos por direito autoral (Lei nº 9.610/1998,
art. 8º, IV).

## Como funciona

1. **map**: lista as normas no catálogo do Senado, com checkpoint por tipo × ano. Um ano que
   volta vazio é consultado de novo no fim da execução e nas execuções seguintes, porque a API
   às vezes responde vazio quando está sobrecarregada.
2. **fetch**: para cada norma, numa única passada, verifica a vigência, descarta as revogadas,
   encontra o texto e baixa o HTML para `data/raw/`. No Planalto, a URL é descoberta testando
   endereços candidatos, e a página que confirma o endereço já é o texto baixado.
3. **parse**: extrai o texto vigente e as anotações, em paralelo nos núcleos da CPU.
4. **dedup**: normaliza o texto, aplica os filtros de qualidade e remove duplicatas.
5. **export**: grava o Parquet, conta os tokens e gera as estatísticas.

O progresso de cada norma em cada etapa fica em um SQLite (`data/state.db`). A execução pode
ser interrompida com Ctrl+C a qualquer momento e retomada com o mesmo comando, sem refazer nem
perder trabalho.

A coleta respeita os servidores. Cada host tem limite próprio de requisições por segundo e de
conexões simultâneas, com intervalos aleatórios entre as requisições. O ritmo cai pela metade
diante de 429, 5xx ou timeout, um circuit breaker pausa o host após falhas seguidas, e a coleta
naquele host para se ele responder com uma página anti-bot.

## Uso

Instalação com [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

Ou com pip: `pip install -r requirements.txt && pip install -e . --no-deps`.

```bash
# Pipeline completo, com as estatísticas no final
uv run legis-dataset run

# Amostra: leis e decretos de 2024, no máximo 20 normas por etapa
uv run legis-dataset run --kinds lei,decreto --years 2024 --limit 20

# Progresso por etapa, com os motivos de falha e de descarte
uv run legis-dataset status

# Estatísticas do dataset já gerado (offline)
uv run legis-dataset stats

# Reabrir falhas permanentes para a próxima execução tentar de novo
uv run legis-dataset retry-failed
```

Cada etapa também roda sozinha: `map`, `fetch`, `parse`, `dedup` e `export`. Durante a
execução, o terminal mostra o progresso de cada etapa e, por host, a taxa atual, as
requisições, os erros e os retries. Os logs detalhados ficam em `data/logs/`.

### Docker

```bash
docker compose build
docker compose run --rm legis-dataset run
docker compose run --rm legis-dataset status
```

Os dados ficam em `./data`, montado como volume, então a execução também é retomável no
container. O cache do tokenizer do Hugging Face fica em `data/hf-cache`.

### Configuração

Os valores padrão estão em [config/default.toml](config/default.toml). Para alterar algum,
crie um TOML só com o que muda e passe com `--config`. Ele é aplicado por cima do padrão. No
Docker, coloque o arquivo em `./data` e use `--config data/meu.toml`.

```toml
[catalog]
norm_kinds = ["lei", "lei.complementar"]
first_year = 1940

[http.hosts."www.planalto.gov.br"]
rate_per_second = 1.5
```

| Chave | Efeito |
|---|---|
| `catalog.norm_kinds`, `first_year`, `last_year` | O que coletar |
| `subsets` | Em qual dataset cada tipo de norma entra |
| `http.hosts.*` | Por host: `rate_per_second` (média), `max_concurrency` e `jitter` (variação de cada intervalo em torno da média; 0,5 = de 50% a 150%) |
| `http.contact` | Contato opcional anexado ao User-Agent |
| `processing.near_duplicate_threshold` | Similaridade a partir da qual dois textos são duplicatas |
| `stats.tokenizer` | Tokenizer do Hugging Face usado na contagem. Se não carregar, a contagem vira uma estimativa e o relatório avisa |

## Desenvolvimento

```
src/legis_dataset/
  sources/     catálogo e localizadores de texto (senado, normas_leg, planalto)
  http/        cliente HTTP, limite por host, circuit breaker, classificação de erros
  parsing/     parsers por fonte, remoção de anotações, montagem do texto
  processing/  normalização, filtros de qualidade, deduplicação
  pipeline/    etapas, execução concorrente, interrupção limpa
  state/       progresso por norma em SQLite
  export/      escrita do Parquet
  stats/       contagem de tokens e estatísticas
  ui/          painel do terminal
```

Para adicionar legislação estadual ou municipal, a jurisdição já faz parte do modelo (`Norm`)
e da URN LexML. Basta registrar um catálogo e/ou localizador em `sources/`
(`@register_catalog`, `@register_locator`), criar o parser em `parsing/` e ativar a fonte em
`[sources]`. Nenhuma etapa do pipeline precisa mudar.

```bash
uv run pytest
```

Os testes usam páginas reais salvas em `tests/fixtures/` e rodam o pipeline inteiro contra
fontes simuladas, sem rede. Os dados gerados em `data/` não são versionados.
