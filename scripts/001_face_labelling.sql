-- Forward-only upgrade for face-group labelling.
-- Apply to an existing catalogue with:
--   psql -v ON_ERROR_STOP=1 "$IMAGELIB_DSN" -f scripts/001_face_labelling.sql
-- Fresh databases receive the same schema from init_db.sql.

\set ON_ERROR_STOP on

BEGIN;

DO $$
DECLARE
    duplicate_labels TEXT;
BEGIN
    SELECT string_agg(
        format('%L (%s rows)', duplicate_name, duplicate_count), ', '
    )
    INTO duplicate_labels
    FROM (
        SELECT name AS duplicate_name, COUNT(*) AS duplicate_count
        FROM persons
        WHERE name IS NOT NULL
        GROUP BY name
        HAVING COUNT(*) > 1
        ORDER BY name
        LIMIT 10
    ) AS duplicates;

    IF duplicate_labels IS NOT NULL THEN
        RAISE EXCEPTION
            'Cannot add uq_persons_name: duplicate non-NULL persons.name values: %',
            duplicate_labels
            USING HINT = 'Resolve duplicate labels manually, then rerun this migration. No rows were renamed or deleted.';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'uq_persons_name'
          AND conrelid = 'persons'::regclass
    ) THEN
        ALTER TABLE persons
            ADD CONSTRAINT uq_persons_name UNIQUE (name);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS face_match_decisions (
    face_id          INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    target_person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE RESTRICT,
    status           TEXT NOT NULL DEFAULT 'proposed',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (face_id, target_person_id),
    CONSTRAINT ck_face_match_decisions_status
        CHECK (status IN ('proposed', 'accepted', 'rejected'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_face_match_decisions_one_accepted_per_face
    ON face_match_decisions (face_id)
    WHERE status = 'accepted';

COMMIT;
