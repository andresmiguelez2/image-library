---
name: db-setup
description: Use when setting up, resetting, debugging, or querying the Postgres + pgvector database for image-library (docker compose, scripts/init_db.sql, HNSW indexes, psql, migrations). Do not use for face-analysis logic or UI queries.
---

# Postgres + pgvector setup

The catalog runs in the `pgvector/pgvector:pg16` container. Schema baseline:
`scripts/init_db.sql`; ORM mirror: `src/imagelib/db/models.py`. The two must
stay in sync (see AGENTS.md).

## Bring it up

```bash
docker compose up -d
docker compose ps          # wait until healthy
```

First boot auto-applies `scripts/init_db.sql` via
`/docker-entrypoint-initdb.d/`. Everything in that mount is a one-shot:
re-running it needs a fresh volume.

DSN: `postgresql+psycopg://imagelib:imagelib@localhost:5432/imagelib`

## Verify artifacts

```bash
docker compose exec postgres psql -U imagelib -d imagelib -c \
  "SELECT extname FROM pg_extension WHERE extname='vector';"
docker compose exec postgres psql -U imagelib -d imagelib -c \
  "SELECT indexname FROM pg_indexes WHERE tablename='faces';"
```

Expected: `vector` extension; HNSW index on `faces` using
`vector_cosine_ops`.

## Reset from scratch

```bash
docker compose down -v && docker compose up -d
```

Wipes data and re-applies init.sql. Only do this after confirming with the
user — it destroys the whole catalog.

## Gotchas

- **Port 5432 conflicts**: an existing local Postgres binds it; change the port
  mapping in `docker-compose.yml` and the DSN together.
- **Vector dimensions**: pgvector's HNSW/IVFFlat indexes cap at 2000 dims.
  Hold the line on 512 (Facenet) for embeddings.