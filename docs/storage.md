# Storage and Caching

Magpie stores search results, fetched snapshots, evidence, run events, and final
answers in a single SQLite database. This documents the schema, the cache reuse
semantics, and source rejection.

## Database

The default path is `~/.local/share/magpie/magpie.db`, configurable via
`database_path`. The database uses WAL journal mode and short-lived connections
(one connection per operation) to remain safe across A2A worker threads.

Connections set `PRAGMA foreign_keys = ON` and `PRAGMA busy_timeout = 10000`.

## Schema Versioning

The schema is versioned via SQLite's `PRAGMA user_version`. The current version
is `4`. On initialization, Magpie checks the existing database's version:

- If the version matches, auxiliary tables are ensured and the database is used
  as-is.
- If the version does not match (incompatible pre-release databases), the
  database file and its `-wal`/`-shm` sidecars are deleted and recreated from
  scratch. Existing data is replaced during initialization.

This means incompatible schema changes during development will destroy existing
data. This is intentional for the pre-release period.

## Tables

| Table              | Purpose                                                        |
| ------------------ | -------------------------------------------------------------- |
| `research_runs`    | One row per research/search/fetch run, with status and timing  |
| `research_queries` | Search queries issued within a run                            |
| `search_results`   | Raw search results per query, including inline content         |
| `documents`        | Fetched page text, deduplicated by content hash               |
| `sources`          | Canonical sources linked to documents, with fetch metadata    |
| `run_source_links` | Many-to-many link between runs and sources                   |
| `evidence_items`   | Excerpts selected from sources for synthesis                  |
| `run_events`       | Append-only per-run event log (stages, errors, milestones)    |
| `final_answers`     | The finalized summary, answer, and references per run         |
| `source_rejections`| Sources rejected for a specific query (auxiliary table)      |

### URL canonicalization

Before storage and deduplication, URLs are canonicalized by:

- Lowercasing the scheme and host.
- Defaulting an empty scheme to `https`.
- Ensuring a path (defaults to `/`).
- Stripping tracking query parameters: `utm_source`, `utm_medium`,
  `utm_campaign`, `utm_term`, `utm_content`, `gclid`, `fbclid`.
- Dropping the fragment.

This makes duplicate results from different tracking campaigns resolve to the
same source.

### Content deduplication

Fetched page text is hashed (`content_hash`, SHA-256 of the stripped text).
The `documents` table enforces a unique constraint on `content_hash`, so pages
with identical text share a document row even when stored under different URLs.
However, URL-specific snapshots remain distinct in `sources` even when their
text is identical — a source is identified by its canonical URL and document.

## Cache Reuse

Exact-question cache reuse is intentionally conservative. When a new run asks
the same normalized question, Magpie looks for previously accepted sources that
are still fresh:

- Only sources cited by completed or partial answers are reusable candidates.
- Sources rejected for a question remain excluded from that question (see
  below).
- Cached canonical URLs are not processed again when search returns duplicates.
- Recent and evergreen questions use separate configurable cache lifetimes
  (`cache_recent_ttl_seconds` and `cache_evergreen_ttl_seconds`).

### Freshness detection

A question is classified as `recent` or `evergreen` in
`detect_freshness_class`. A question is `recent` if it contains any of:

> `latest`, `current`, `today`, `yesterday`, `this week`, `this month`,
> `this year`

…or if it mentions a year within one year of the current year (matched as
`\b20\d{2}\b`). Otherwise it is `evergreen`. The freshness class determines
which cache TTL applies.

## Source Rejection

Source acceptance is a structured resolver decision. When a synthesis round
reports that its evidence does not answer the question (`source_answers_question`
is false), each source in that round is recorded in the `source_rejections`
table against the normalized query. Those sources are then excluded from future
exact-query cache reuse for that question.

Magpie does not attempt to infer whether an answer is a refusal by matching
generated prose with regex. Acceptance and rejection are explicit fields in the
resolver's structured output.
