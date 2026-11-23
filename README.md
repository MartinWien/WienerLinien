# wl-pulse

![CI](https://github.com/MartinWien/WienerLinien/actions/workflows/ci.yml/badge.svg)

Live departures, delays and disruptions for Vienna public transport, built on the
[Wiener Linien open data feeds](https://www.data.gv.at/datasets/cfba4373-a654-3e0b-80f8-348738169f95).
A background poller collects the real-time data; a FastAPI backend serves it through a REST API
and a built-in departure board web UI (German). The service keeps answering — with an explicit
`stale` flag — even when the upstream feed is down.

**Live demo:** <https://wienerlinien.onrender.com> (free Render instance: the first request
after a pause can take a minute or so while the container starts).

## Stack

- **Backend:** Python 3.11+, FastAPI, SQLAlchemy 2 (async), httpx, pydantic v2
- **Storage:** PostgreSQL (SQLite fallback for zero-config local runs)
- **Cache:** Redis (in-memory fallback)
- **Frontend:** plain HTML/CSS/JavaScript, no build step
- **Tooling:** pytest, ruff, mypy (strict), Docker + docker-compose, GitHub Actions

## How the backend works

```
Wiener Linien CSVs ──► Registry loader ──► DB (stations, lines, stops)
Wiener Linien API  ──► Poller (async task, every 30 s)
                          ├─► Cache: latest departures per station (TTL)
                          └─► DB: bounded observation history (for stats)
                                        │
Clients ◄── REST API / Web UI ◄─────────┘   (reads cache/DB only)
```

1. **Registry loader** — parses the four static CSVs (stations, stop points, lines, route
   courses) into a normalized schema. One station maps to many RBL stop IDs (one per
   platform/line/direction). Idempotent, auto-downloads the CSVs on first start.
2. **Poller** — a single asyncio background task. Batches all watched RBLs into as few monitor
   requests as possible, normalizes the responses, writes the latest snapshot to the cache and
   appends observations to a retention-bounded history table. Stations are watched on demand:
   requesting an unwatched station adds it to the poll set (LRU cap + TTL), so any station in
   Vienna works.
3. **Read API** — serves everything from cache/DB and never calls the upstream in a request.
   If the feed is down, the last known data is returned with `stale: true` instead of a 5xx.

Resilience: per-request timeouts, retries with jittered exponential backoff, a circuit breaker
(open after N failed cycles, half-open probe after a cooldown), TTL cache with DB fallback,
per-IP rate limiting, and JSON logs correlated by request ID and poll-cycle ID. The Wiener
Linien fair-use minimum of 15 s between polls is enforced in the settings layer (values below
are clamped), default interval 30 s.

## API

| Endpoint | Description |
| --- | --- |
| `GET /` | Departure board web UI |
| `GET /stations?search=&limit=&offset=` | Station search, relevance-ranked, paginated |
| `GET /stations/{id}/departures` | Upcoming departures (countdown, planned vs. realtime, delay) |
| `GET /stations/{id}/stats?window=60` | Delay stats per line: on-time %, avg/max drift |
| `GET /stations/{id}/predict?hour=&line=` | Expected delay per line/hour (rolling statistical baseline) |
| `GET /disruptions` | Active disruptions from the feed |
| `GET /health` | DB, cache, poller and circuit-breaker status |
| `GET /docs` | OpenAPI UI |

Every response carries a `meta` block with data attribution, fetch timestamp and `stale` flag.
Station IDs are DIVA numbers from the registry (Karlsplatz = `60200657`).

## Run it

**Docker (app + PostgreSQL + Redis):**

```bash
docker compose up --build
# UI:  http://localhost:8000/      API docs:  http://localhost:8000/docs
```

**Local, zero infrastructure (SQLite + in-memory cache):**

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

The registry CSVs (~5 MB) are downloaded automatically on first start.
All configuration is via `WL_`-prefixed environment variables with working defaults —
see [.env.example](.env.example).

**Deployment:** the app is one always-running container (the poller lives in the same
process), so it needs a container platform — Render ([render.yaml](render.yaml) included),
Railway or Fly.io. Serverless platforms like Vercel won't work: they freeze the process
between requests, which kills the poller, and their read-only filesystem breaks the
registry download.

## Development

```bash
pytest                  # full suite, no network access (upstream is mocked)
ruff check . && ruff format --check .
mypy app tests
```

Tests cover the failure paths: upstream timeouts serving stale cache, circuit breaker
open/half-open/close, malformed payloads, cache TTL and DB fallback, retention cleanup,
dynamic watchlist eviction, pagination and rate limiting.

## Data & license

Code: MIT. Transport data: **Datenquelle: Stadt Wien – data.wien.gv.at (Wiener Linien),
CC BY 4.0** — attributed in every API response, the UI and this README. Polling respects the
published fair-use terms (minimum 15 s interval, batched requests, backoff on failures).
