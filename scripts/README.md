# Database scripts

`init_db.sql` is the schema baseline for fresh databases and is applied on
first container startup. Existing databases must not be reset to pick up schema
changes. Apply forward-only upgrades explicitly, for example:

```bash
export IMAGELIB_DSN='postgresql://imagelib:imagelib@localhost:5432/imagelib'
psql -v ON_ERROR_STOP=1 "$IMAGELIB_DSN" -f scripts/001_face_labelling.sql
psql -v ON_ERROR_STOP=1 "$IMAGELIB_DSN" -f scripts/002_content_locations.sql
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

`002_content_locations.sql` upgrades the old path-keyed `images` table to one
logical `Image` per unique SHA-256 `content_hash`, with each `(source_id, path)`
stored in `image_locations`. It keeps the lowest existing image ID for each
hash, moves all locations to it, and reassigns (without recreating) any faces so
face IDs, person assignments, cover faces, and match decisions remain intact.
It aborts transactionally before changing rows if a hash group has faces on
multiple old image rows, has inconsistent `face_count` values, contains a
duplicate non-SHA-256 placeholder, or has conflicting non-NULL content
metadata. Resolve those groups explicitly before retrying; this migration does
not infer face correspondence or discard conflicting data. Status is merged to the furthest
successful pipeline state (`analysed`, then `indexed`, then `pending`, then
`error`); an error is retained only when every duplicate row is in the error
state. The new location API is `Image.locations` / `Source.locations`, with
`ImageLocation(image_id, source_id, path)` as the row-level model.

The migration is a one-time forward upgrade from the old schema. Run it
explicitly against the intended catalogue after taking the normal database
backup; do not reset an existing database. `init_db.sql` remains the baseline
for fresh databases and already creates the destination schema.

The ORM and schema constraints are covered with SQLite tests. The migration
preflights use PostgreSQL-specific PL/pgSQL and are not executed by the unit
suite; validate the scripts against a disposable PostgreSQL database before
production rollout. Do not use `docker compose down -v` on a database with
catalogue data.
