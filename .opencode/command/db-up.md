---
description: Start the Postgres+pgvector container and wait for it to be healthy.
agent: db-admin
---

Bring up the catalog database:

1. Run `docker compose up -d`.
2. Wait until the container reports healthy: `docker compose ps`.
3. Confirm connectivity: `docker compose exec postgres pg_isready -U imagelib -d imagelib`.

If you see the "docker command could not be found in this WSL 2 distro" error,
stop: the user must enable Docker Desktop → Settings → Resources → WSL
integration for this distro first.