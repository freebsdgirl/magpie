# Magpie

Magpie is a natural-language information retrieval agent built for delegation
from another agent. It answers general questions with bounded web research and
routes requests such as weather lookups to more appropriate APIs when possible.

It exposes an A2A interface for agent-to-agent use and a local CLI for direct
queries. Results are compact, structured, grounded in references, and suitable
for a conversational agent to present in its own voice.

## Design Philosophy

General-purpose conversational agents should not need to carry every tool,
provider, and lookup workflow in their prompt. That becomes especially slow
and unreliable when the system is backed by a smaller local model.

Magpie keeps that work inside a dedicated information-retrieval agent:

- The upstream agent delegates a plain-language question.
- A small routing decision chooses general web research or a specialized path.
- Deterministic code handles provider calls, budgets, caching, and validation.
- The resolver receives all gathered evidence at once per round, not one source
  at a time.
- The upstream agent receives a grounded answer and references, then applies
  personality or continues the conversation.

The goal is not to make an LLM imitate a search engine. The goal is to give a
smaller model a constrained workflow in which it can make useful decisions
without drowning in context.

## What It Does

- answers natural-language questions using web search and fetched pages
- returns essay-style answers for explanatory questions with grounded references
- gathers multiple sources per research round and synthesizes them in one call
- routes current-condition and forecast requests to the Neon Hail weather API
- answers anime facts, Japanese voice-cast questions, and local-time airing
  schedules through AniList
- returns compact category news digests from publisher RSS and Atom feeds
- exposes indexed search results without synthesis via the magpie_search skill
- fetches web page content by index or URL via the magpie_fetch skill
- caches useful source snapshots and completed answers in SQLite
- exposes durable, cancellable ask runs through A2A
- records resolver, fetch, timing, and run diagnostics for debugging

## Requirements

- Python 3.12+
- an OpenAI-compatible local or remote model endpoint
- Crawl4AI and its browser assets for page fetching
- network access to the configured search provider

The default configuration uses Exa MCP for search, Crawl4AI for page fetching,
and an OpenAI-compatible resolver at `http://localhost:11434/v1`.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
crawl4ai-setup
cp config.example.json config.json
```

Edit `config.json` to select the resolver model and any provider credentials,
then check the environment:

```bash
magpie doctor --live
```

## Configuration

Config resolution order:

1. the path passed with `--config`
2. `./config.json`
3. `~/.config/magpie/config.json`
4. built-in defaults

See `config.example.json` for the full schema. Every setting also supports an
environment-variable override using the `MAGPIE_` prefix. Common settings
include:

- `MAGPIE_RESOLVER_BASE_URL`
- `MAGPIE_RESOLVER_MODEL`
- `MAGPIE_RESOLVER_API_KEY`
- `MAGPIE_SEARCH_API_KEY`
- `MAGPIE_DATABASE_PATH`
- `MAGPIE_A2A_BASE_URL`
- `MAGPIE_WEATHER_ENABLED`
- `MAGPIE_NEWS_ENABLED`
- `MAGPIE_HISTORIAN_ENABLED`
- `MAGPIE_HISTORIAN_BASE_URL`
- `MAGPIE_HISTORIAN_TOKEN`

## Run

Start the A2A server:

```bash
magpie serve
```

Ask questions from the CLI:

```bash
magpie ask "Who is the mayor of New York?"
magpie ask "How do I make homemade sourdough bread?"
magpie ask "What's the weather in 98230?"
magpie ask "Give me the forecast for 98230" --json
magpie ask "Who voices Kirishima in Yakuza Fiancé?"
magpie ask "anime schedule for today"
magpie ask "What's the latest AI news?"
magpie ask "world news from yesterday" --json
magpie ask "Compare the latest policies" --json --debug
```

Search the web and get indexed results with summaries:

```bash
magpie search "a2a protocol"
magpie search "rust borrow checker" --max-results 3 --json
```

Fetch web page content by index or URL:

```bash
magpie fetch 0 --run-id <run_id_from_search>
magpie fetch "https://example.com/article"
magpie fetch 2 --run-id <run_id> --full
```

Other useful commands:

```bash
magpie doctor --live
magpie clear-cache
```

`magpie ask` first tries the configured local A2A server. If initial A2A
discovery or connection fails, it runs the same service directly. It does not
silently retry a request after the A2A server has accepted it.

## A2A Usage

Magpie exposes three skills on its agent card:

### magpie_ask

Synthesizes a grounded answer from web search. Sends `skill: "magpie_ask"`
(or omit the skill — it is the default). Returns:

- `summary`: a compact tool-friendly description
- `answer`: the grounded answer (essay-style for explanatory questions)
- `references`: sources used by the answer
- `warnings` and `limitations`: relevant caveats
- `status`, `stop_reason`, and `run_id`: execution state

### magpie_search

Returns indexed search results with short summaries and source URLs. No
model synthesis. Sends `skill: "magpie_search"`. Returns:

- `run_id`: used for follow-up fetch calls
- `query`: the refined search query
- `results`: array of `{ index, title, url, site_name, published_at, summary }`

### magpie_fetch

Retrieves full web page content by index (from a prior search) or by URL.
Sends `skill: "magpie_fetch"` with either an index + `run_id` in metadata,
or a URL. By default returns stored Exa content (instant); set `full: true`
in metadata to force a fresh crawl4ai fetch. Returns:

- `run_id`, `index`, `url`, `title`, `content`, `fetched_via`, `warnings`

Published endpoints include:

- `POST /a2a`
- `GET /.well-known/agent-card.json`
- standard A2A REST task routes
- `GET /healthz`

A2A task IDs are also durable Magpie run IDs, so task cancellation targets the
same run recorded in SQLite.

## How It Works

Every request begins with a compact resolver routing call. Weather requests
with a confident five-digit US ZIP code go directly to Neon Hail and bypass web
search and synthesis. Anime requests are classified a second time into factual
lookup, credits, or schedule operations and then sent to AniList. For factual
lookups, the resolver selects from an allowlist of API fields; deterministic
code builds the GraphQL query and returns only the requested values. Daily anime
schedules use Japanese broadcast times converted to the system timezone.
Jikan is used only as a fallback title-discovery index when AniList cannot
resolve a spelling variant; final anime data and references still come from
AniList. Broad category news requests are classified a second time into a
category and strict local-time window, then answered directly from configured
RSS or Atom feeds without article fetching or synthesis. Arbitrary topics such
as company-specific news fall back to general web research. If a specialized
route fails, Magpie falls back to general web research.

General web lookup follows a bounded batch loop:

1. Reuse fresh, previously accepted sources for the exact question when available.
2. Ask the resolver for one focused search query.
3. Search, deduplicate canonical URLs, and gather a limited source set.
4. Use Exa inline content directly when available; fall back to Crawl4AI only
   when inline content is absent or too short.
5. Pass all evidence from the round to the resolver in one synthesis call.
6. Continue with the next query only when questions remain.

Resolver calls are serialized across concurrent runs because the expected
deployment target is a smaller local model, not a high-throughput frontier API.

Each research round gathers multiple sources before synthesizing. The resolver
receives all evidence items from the round at once, along with any prior draft,
and writes a thorough answer covering the relevant facets of the topic
(background, purpose, key components, how it works). The synthesis prompt
prefers several substantive paragraphs over a single terse paragraph. When
sources present competing complete options (for example, different recipes),
the resolver commits to the single best one rather than surveying alternatives.
Specialized routes (weather, anime, news) bypass synthesis entirely and answer
directly.

## Grounding And Cache Behavior

Search results, fetched snapshots, evidence, run events, and final answers are
stored in SQLite. URL-specific snapshots remain distinct even when their text is
identical.

Exact-question cache reuse is intentionally conservative:

- only sources cited by completed or partial answers are reusable candidates
- sources rejected for a question remain excluded from that question
- cached canonical URLs are not processed again when search returns duplicates
- recent and evergreen questions use separate configurable cache lifetimes

Source acceptance is a structured resolver decision. Magpie does not attempt to
infer whether an answer is a refusal by matching generated prose with regex.

## Bounded Lookup

Runs are limited across queries, sources, evidence items, source characters, and
incremental answer size. The principal settings are:

- `max_search_queries_per_run`
- `max_search_results_per_query`
- `max_sources_per_query`
- `max_sources_per_run`
- `max_evidence_items_per_run`
- `max_evidence_characters_per_item`
- `max_synthesis_input_characters`
- `max_incremental_answer_characters`
- `resolver_max_tokens`

Completed answers return `status: "ok"`. Grounded answers that stop before all
remaining questions are resolved return `status: "partial"`. Runs without a
usable grounded answer return `status: "error"`.

## Diagnostics

Use `magpie ask ... --debug` or enable `include_timing_debug` to include
timings and run events in results. Shared resolver and fetch logs are tagged
with run IDs:

- `~/.local/share/magpie/magpie-resolver.log`
- `~/.local/share/magpie/magpie-fetch.log`

Raw model output is written to the resolver log only when
`resolver_include_raw_output` is enabled. Logs may contain full prompts, source
content, and model output; do not publish them without reviewing their contents.

## Historian Integration

Historian event production is optional and disabled by default.
[Historian](https://github.com/freebsdgirl/historian) is the event sink Magpie
can publish to for research runs, routes, queries, sources, synthesis, and
failure activity. Install `historian.manifest.json` with Historian, store the
printed token in `MAGPIE_HISTORIAN_TOKEN`, and set
`MAGPIE_HISTORIAN_ENABLED=true`. Historian delivery failures are logged but
never change a successful research result into a failure.

Install the bundled manifest from the Historian checkout:

```console
historian app install /path/to/magpie/historian.manifest.json
```

The token may also be stored as `historian_token` in Magpie's configuration.
The default endpoint is `http://127.0.0.1:8768`. Delivery retries connection
failures and HTTP 5xx responses, but no durable client-side spool is created.

Magpie emits compact lifecycle records after its normal durable transitions. It
does not send fetched documents, answer prose, raw provider payloads, hidden
model reasoning, or credentials.

## Development Notes

- Keep resolver prompts and decision surfaces small. More choices can make a
  local model slower and less reliable even when prompt ingestion is fast.
- Prefer structured model decisions and deterministic validation over prose
  interpretation.
- Specialized API routes should bypass general research when they can produce a
  better grounded result.
- The built-in RSS registry is intended for local or personal aggregation.
  Check publisher terms before redistributing feed content.
- API clients should request and retain only fields needed for the final answer;
  provider metadata must not leak into resolver prompts or user-facing output.
- Do not cache rejected sources as answer candidates.
- Do not silently retry requests that may already have been accepted.
- The SQLite schema is versioned. Incompatible pre-release databases may be
  replaced during initialization.

Run the test suite with:

```bash
python -m unittest discover -s tests -v
# or, when pytest is installed
pytest
```
