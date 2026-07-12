# data/

Runtime data directory (contents are gitignored):

- `wienerlinien-ogd-*.csv` — the four static registry files. Downloaded
  automatically on first start (`WL_REGISTRY_AUTO_DOWNLOAD=true`, the default)
  or manually via `python -m app.registry.download`.
- `wl.db` — the local SQLite database when no PostgreSQL is configured.

Datenquelle: Stadt Wien - data.wien.gv.at (Wiener Linien), CC BY 4.0.
