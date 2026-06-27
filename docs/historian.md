# Historian Integration

Historian event production is optional and disabled by default.
[Historian](https://github.com/freebsdgirl/historian) is the event sink Magpie
can publish to for research runs, routes, queries, sources, synthesis, and
failure activity.

## Setup

1. Install the bundled manifest from the Historian checkout:

   ```console
   historian app install /path/to/magpie/historian.manifest.json
   ```

2. Store the printed token. You can set it in Magpie's configuration as
   `historian_token`, or via the `MAGPIE_HISTORIAN_TOKEN` environment variable.

3. Enable integration:

   - Set `historian_enabled: true` in `config.json`, or
   - Set `MAGPIE_HISTORIAN_ENABLED=true`.

   When `historian_enabled` is true, `historian_token` is required; otherwise
   config validation fails at load time.

The default Historian endpoint is `http://127.0.0.1:8768`, configurable via
`historian_base_url`.

## What Is Emitted

Magpie emits compact lifecycle records after its normal durable transitions. The
event types are:

| Event type                     | When emitted                              |
| ------------------------------- | ------------------------------------------ |
| `research.run.started`          | A run begins                                |
| `research.run.completed`        | A run finishes with `status: ok`           |
| `research.run.partial`          | A run finishes with `status: partial`      |
| `research.run.failed`           | A run finishes with `status: error`         |
| `research.run.canceled`         | A run is cancelled via A2A                  |
| `research.route.selected`       | A route (weather/anime/news/web) is chosen  |
| `research.query.executed`       | A search query completes                    |
| `research.source.discovered`    | A new source is found in search results     |
| `research.source.fetched`       | A source's content is acquired              |
| `research.source.rejected`      | A source is rejected for a query             |
| `research.synthesis.completed`  | A synthesis call completes                  |

## What Is Not Emitted

Magpie does not send:

- fetched documents or page content,
- answer prose or synthesized text,
- raw provider payloads,
- hidden model reasoning,
- credentials.

Event data is sanitized before emission: known secrets (`search_api_key`,
`resolver_api_key`, `historian_token`) are replaced with `[REDACTED]` if they
appear anywhere in the payload.

## Failure Handling

Historian delivery failures are logged but never change a successful research
result into a failure. A failed `emit` is caught, logged at WARNING level with
the event ID and type, and the run continues normally.

Delivery retries connection failures and HTTP 5xx responses up to
`historian_retry_count` times (default `2`). No durable client-side spool is
created — if Historian is unreachable after retries, the events are dropped.
