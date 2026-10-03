# Database scripts

`init_db.sql` is the schema baseline for fresh databases and is applied on
first container startup. Existing databases must not be reset to pick up schema
changes. Apply forward-only upgrades explicitly, for example:

```bash
export IMAGELIB_DSN='postgresql://imagelib:imagelib@localhost:5432/imagelib'
psql -v ON_ERROR_STOP=1 "$IMAGELIB_DSN" -f scripts/001_face_labelling.sql
```

The migration adds the unique constraint on non-NULL person names and the
`face_match_decisions` table. It checks for duplicate labels before changing
the schema and aborts with the conflicting labels; resolve duplicates manually
and rerun it. It does not rename or delete catalogue rows. It can safely be
rerun after a successful application.

Decision rows are keyed by `(face_id, target_person_id)`, with status
`proposed`, `accepted`, or `rejected`. The face FK follows the stable `faces.id`
and cascades only when that face is deleted; the target FK points at `persons.id`
and restricts deletion. The service API must only use a target Person whose
`name` is non-NULL; PostgreSQL cannot express that cross-row condition as a
normal foreign key/check constraint. At most one target per face can be
`accepted`, while proposals and rejections for other targets are retained.

The ORM and schema constraints are covered with SQLite tests. The migration's
duplicate-label preflight uses PostgreSQL-specific PL/pgSQL and is not executed
by the unit suite; validate it against a disposable PostgreSQL database before
production rollout. Do not use `docker compose down -v` on a database with
catalogue data.
