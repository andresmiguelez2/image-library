---
description: Apply scripts/init_db.sql and verify the pgvector extension and HNSW index.
agent: db-admin
---

Ensure the DB container is up (`docker compose up -d`), then apply the schema:

```
docker compose exec -T postgres psql -U imagelib -d imagelib -f /docker-entrypoint-initdb.d/init.sql
```

Verify the artifacts exist:

```
docker compose exec postgres psql -U imagelib -d imagelib -c \
  "SELECT extname FROM pg_extension WHERE extname='vector'; \
   SELECT indexname FROM pg_indexes WHERE tablename='faces';"
```

Expected: `vector` extension present and an HNSW index on `faces`. To start
from scratch: `docker compose down -v && docker compose up -d` (re-runs
init.sql automatically), but only after confirming with the user.