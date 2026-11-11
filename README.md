# dataset-juridico

Pipeline em Python que gera um dataset de **legislação federal brasileira vigente** para
*Continuous Pre-Training* (CPT) de LLMs jurídicos, coletado **direto das fontes oficiais**.

- Só o **texto em vigor**: normas revogadas são descartadas e dispositivos revogados/vetados
  são removidos.
- Anotações editoriais ("(Redação dada pela Lei nº …)", "(Incluído pela…)", "(Vide…)") saem
  do texto e ficam no campo `annotations`.
- Saída final em **Parquet**, em dois datasets: `leis` (CF, emendas, leis complementares e
  ordinárias, decretos-lei, MPs) e `decretos`.
- **Retomável**: pode ser interrompido (Ctrl+C) e reexecutado; continua de onde parou.
- **Educado com os servidores**: limite de requisições e de conexões por host, backoff
  adaptativo, circuit breaker e parada imediata se um host responder com página anti-bot.

## Fontes

| Fonte | Uso |
|---|---|
| [Dados Abertos do Senado](https://legis.senado.leg.br/dadosabertos) | Catálogo das normas (por tipo e ano) e situação (revogada ou não) |
| [normas.leg.br](https://normas.leg.br) (Congresso Nacional) | Vigência e **texto compilado vigente** (CC BY 4.0) |
| [Planalto](https://www.planalto.gov.br/ccivil_03) | Texto quando o normas.leg.br não tem compilação (principalmente decretos); texto riscado é removido |

Textos de leis e atos oficiais não são protegidos por direito autoral
(Lei nº 9.610/1998, art. 8º, IV).

## Instalação

Com [uv](https://docs.astral.sh/uv/) (recomendado):

```bash
uv sync
```

Ou com pip:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e . --no-deps
```

## Uso

```bash
# Pipeline completo (map → fetch → parse → dedup → export) + estatísticas no final
uv run legis-dataset run

# Teste rápido: só leis e decretos de 2024, no máximo 20 normas por etapa
uv run legis-dataset run --kinds lei,decreto --years 2024 --limit 20

# Só as estatísticas do dataset já gerado (offline)
uv run legis-dataset run --stats-only      # ou: uv run legis-dataset stats

# Progresso por etapa e motivos de falha/descarte
uv run legis-dataset status

# Reabrir falhas permanentes para a próxima execução tentar de novo
uv run legis-dataset retry-failed
```

Cada etapa também roda sozinha: `map`, `fetch`, `parse`, `dedup`, `export`. Durante a execução
o terminal mostra qual fonte está sendo consultada, o progresso de cada etapa e, por host,
taxa atual, requisições, erros e retries. Logs detalhados ficam em `data/logs/`.

## Etapas

1. **map** — enumera as normas no catálogo do Senado (checkpoint por tipo × ano) e, para cada
   norma, descobre se está em vigor e onde está o texto (normas.leg.br → Planalto). Gera
   `data/reports/source_map.json`.
2. **fetch** — baixa o HTML bruto (`data/raw/`, gzip), com GET condicional (ETag) nas reexecuções.
3. **parse** — extrai o texto limpo (um dispositivo por linha) e as anotações
   (`data/interim/parsed/`), em paralelo nos núcleos da CPU.
4. **dedup** — normalização, filtros de qualidade e deduplicação exata (sha256) e aproximada
   (MinHash) (`data/interim/clean/`).
5. **export** — Parquet com contagem de tokens (`data/final/leis/`, `data/final/decretos/`) e
   `data/final/stats.json`.

## Formato do Parquet

| Coluna | Descrição |
|---|---|
| `id` | URN LexML, ex. `urn:lex:br:federal:lei:1990-09-11;8078` |
| `text` | Texto vigente, um dispositivo por linha |
| `title`, `ementa` | Título e ementa |
| `tipo`, `numero`, `data_assinatura` | Identificação da norma |
| `esfera`, `uf`, `municipio` | Jurisdição (hoje sempre `federal`) |
| `fonte`, `url`, `versao_texto` | De onde veio o texto (`current`, `compiled` ou `original`) |
| `annotations` | Lista de `{tipo, texto, norma_ref}` removidos do texto |
| `n_chars`, `n_palavras`, `n_tokens` | Tamanhos (tokens pelo tokenizer configurado) |
| `sha256`, `data_coleta` | Hash do texto e data do download |

## Configuração

Tudo fica em [config/default.toml](config/default.toml). Para mudar, copie o arquivo e passe com
`--config`. Os principais ajustes são:

- `catalog.norm_kinds`, `first_year`, `last_year`: o que coletar.
- `subsets`: em qual dataset cada tipo de norma entra.
- `http.hosts.*`: requisições/s e conexões simultâneas por host.
- `http.contact`: contato opcional anexado ao User-Agent.
- `stats.tokenizer`: tokenizer do Hugging Face usado para contar tokens. Use o do modelo que
  será treinado. Se ele não puder ser carregado, a contagem vira uma estimativa e o relatório
  avisa isso.

## Arquitetura

```
src/legis_dataset/
  cli.py, config.py, models.py, logging_setup.py
  state/       SQLite com o progresso de cada norma em cada etapa (base da retomada)
  http/        cliente HTTP, limitador por host, circuit breaker, classificação de erros
  sources/     catálogo e localizadores de texto (senado, normas_leg, planalto) + registro
  parsing/     parsers por fonte, remoção de anotações e montagem do texto
  processing/  normalização, filtros de qualidade, deduplicação
  export/      escrita do Parquet
  stats/       contagem de tokens, estatísticas e relatório
  pipeline/    etapas, execução concorrente e interrupção limpa
  ui/          painel do terminal
```

**Legislação estadual/municipal**: `Norm` já carrega a jurisdição (`esfera`, `uf`, `municipio`)
e a URN segue o padrão LexML. Para adicionar uma fonte nova:

1. Crie um catálogo e/ou localizador em `sources/` com `@register_catalog`/`@register_locator`.
2. Crie um parser em `parsing/`.
3. Ative a fonte em `[sources]` no config.

Nenhuma etapa do pipeline precisa mudar.

## Testes

```bash
uv run pytest
```

Os testes usam páginas reais salvas em `tests/fixtures/` e rodam o pipeline inteiro com as
fontes simuladas (sem rede).

## Dados

Tudo o que é gerado fica em `data/` e **não é versionado** (ver `.gitignore`).
