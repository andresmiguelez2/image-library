-- Forward-only upgrade from path-keyed images to content-keyed images.
-- Apply once to an existing catalogue with:
--   psql -v ON_ERROR_STOP=1 "$IMAGELIB_DSN" -f scripts/002_content_locations.sql
-- Fresh databases receive the destination schema from init_db.sql.

\set ON_ERROR_STOP on

BEGIN;

DO $$
DECLARE
    ambiguous_face_groups TEXT;
    conflicting_metadata TEXT;
    invalid_hash_groups TEXT;
    inconsistent_face_counts TEXT;
BEGIN
    SELECT string_agg(
        format('hash %L on image IDs [%s]', content_hash, image_ids), '; '
    )
    INTO invalid_hash_groups
    FROM (
        SELECT
            content_hash,
            string_agg(id::TEXT, ', ' ORDER BY id) AS image_ids
        FROM images
        GROUP BY content_hash
        HAVING COUNT(*) > 1
           AND content_hash !~ '^[[:xdigit:]]{64}$'
        ORDER BY content_hash
        LIMIT 10
    ) AS invalid_duplicate_hashes;

    IF invalid_hash_groups IS NOT NULL THEN
        RAISE EXCEPTION
            'Cannot merge duplicate values that are not SHA-256 hashes: %',
            invalid_hash_groups
            USING HINT = 'Resolve placeholder or malformed hashes before rerunning. No rows were changed.';
    END IF;

    SELECT string_agg(
        format('hash %L on image IDs [%s]', content_hash, image_ids), '; '
    )
    INTO ambiguous_face_groups
    FROM (
        SELECT
            i.content_hash,
            string_agg(i.id::TEXT, ', ' ORDER BY i.id) AS image_ids
        FROM images AS i
        WHERE EXISTS (SELECT 1 FROM faces AS f WHERE f.image_id = i.id)
        GROUP BY i.content_hash
        HAVING COUNT(*) > 1
        ORDER BY i.content_hash
        LIMIT 10
    ) AS duplicate_face_sets;

    IF ambiguous_face_groups IS NOT NULL THEN
        RAISE EXCEPTION
            'Cannot merge duplicate content hashes with multiple image rows containing faces: %',
            ambiguous_face_groups
            USING HINT = 'Resolve or explicitly reconcile these face sets and their labels before rerunning. No rows were changed.';
    END IF;

    SELECT string_agg(
        format('hash %L on image ID %s (face_count %s, stored faces %s)',
               content_hash, image_id, recorded_count, stored_count), '; '
    )
    INTO inconsistent_face_counts
    FROM (
        SELECT
            i.content_hash,
            i.id AS image_id,
            i.face_count AS recorded_count,
            (SELECT COUNT(*)::INTEGER FROM faces AS f WHERE f.image_id = i.id) AS stored_count
        FROM images AS i
        JOIN (
            SELECT content_hash
            FROM images
            GROUP BY content_hash
            HAVING COUNT(*) > 1
        ) AS duplicate_hashes USING (content_hash)
        WHERE i.face_count <> (
            SELECT COUNT(*)::INTEGER FROM faces AS f WHERE f.image_id = i.id
        )
        ORDER BY i.content_hash, i.id
        LIMIT 10
    ) AS face_count_mismatches;

    IF inconsistent_face_counts IS NOT NULL THEN
        RAISE EXCEPTION
            'Cannot merge duplicate content hashes with inconsistent face counts: %',
            inconsistent_face_counts
            USING HINT = 'Reconcile the stored faces and face_count values before rerunning. No rows were changed.';
    END IF;

    SELECT string_agg(
        format('hash %L on image IDs [%s]', content_hash, image_ids), '; '
    )
    INTO conflicting_metadata
    FROM (
        SELECT
            content_hash,
            string_agg(id::TEXT, ', ' ORDER BY id) AS image_ids
        FROM images
        GROUP BY content_hash
        HAVING COUNT(*) > 1
           AND (
                COUNT(DISTINCT size_bytes) > 1
             OR COUNT(DISTINCT width) > 1
             OR COUNT(DISTINCT height) > 1
             OR COUNT(DISTINCT taken_at) > 1
             OR COUNT(DISTINCT gps_lat) > 1
             OR COUNT(DISTINCT gps_lon) > 1
             OR COUNT(DISTINCT gps_place) > 1
             OR COUNT(DISTINCT camera_make) > 1
             OR COUNT(DISTINCT camera_model) > 1
             OR COUNT(DISTINCT thumb_path) > 1
             OR COUNT(DISTINCT error) > 1
           )
        ORDER BY content_hash
        LIMIT 10
    ) AS conflicting_groups;

    IF conflicting_metadata IS NOT NULL THEN
        RAISE EXCEPTION
            'Cannot merge duplicate content hashes with conflicting non-NULL metadata: %',
            conflicting_metadata
            USING HINT = 'Inspect the listed rows and reconcile the metadata manually before rerunning. No rows were changed.';
    END IF;
END
$$;

CREATE TABLE image_locations (
    id         SERIAL PRIMARY KEY,
    image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    CONSTRAINT uq_image_locations_path UNIQUE (path)
);

CREATE INDEX ix_image_locations_image_id ON image_locations (image_id);
CREATE INDEX ix_image_locations_source_id ON image_locations (source_id);

-- Keep the lowest image ID as the stable logical image ID. Every old path and
-- source becomes a location on that image; face IDs and their label references
-- are preserved by moving, not recreating, faces.
WITH canonical AS (
    SELECT id, MIN(id) OVER (PARTITION BY content_hash) AS survivor_id
    FROM images
)
INSERT INTO image_locations (image_id, source_id, path)
SELECT canonical.survivor_id, old_image.source_id, old_image.path
FROM images AS old_image
JOIN canonical ON canonical.id = old_image.id
ORDER BY old_image.id;

WITH canonical AS (
    SELECT id, MIN(id) OVER (PARTITION BY content_hash) AS survivor_id
    FROM images
)
UPDATE faces AS face
SET image_id = canonical.survivor_id
FROM canonical
WHERE face.image_id = canonical.id
  AND face.image_id <> canonical.survivor_id;

WITH merged AS (
    SELECT
        content_hash,
        MIN(id) AS survivor_id,
        CASE
            WHEN BOOL_OR(status = 'analysed') THEN 'analysed'
            WHEN BOOL_OR(status = 'indexed') THEN 'indexed'
            WHEN BOOL_OR(status = 'pending') THEN 'pending'
            ELSE 'error'
        END AS merged_status,
        CASE
            WHEN BOOL_OR(status <> 'error') THEN NULL
            ELSE MAX(error)
        END AS merged_error,
        MAX(size_bytes) AS size_bytes,
        MAX(width) AS width,
        MAX(height) AS height,
        MAX(taken_at) AS taken_at,
        MAX(modified_at) AS modified_at,
        MAX(gps_lat) AS gps_lat,
        MAX(gps_lon) AS gps_lon,
        MAX(gps_place) AS gps_place,
        MAX(camera_make) AS camera_make,
        MAX(camera_model) AS camera_model,
        MAX(thumb_path) AS thumb_path,
        MIN(created_at) AS created_at,
        MAX(updated_at) AS updated_at
    FROM images
    GROUP BY content_hash
    HAVING COUNT(*) > 1
)
UPDATE images AS survivor
SET size_bytes = merged.size_bytes,
    width = merged.width,
    height = merged.height,
    taken_at = merged.taken_at,
    modified_at = merged.modified_at,
    gps_lat = merged.gps_lat,
    gps_lon = merged.gps_lon,
    gps_place = merged.gps_place,
    camera_make = merged.camera_make,
    camera_model = merged.camera_model,
    thumb_path = merged.thumb_path,
    face_count = (
        SELECT COUNT(*)::INTEGER FROM faces WHERE faces.image_id = survivor.id
    ),
    status = merged.merged_status,
    error = merged.merged_error,
    created_at = merged.created_at,
    updated_at = merged.updated_at
FROM merged
WHERE survivor.id = merged.survivor_id;

WITH canonical AS (
    SELECT id, MIN(id) OVER (PARTITION BY content_hash) AS survivor_id
    FROM images
)
DELETE FROM images AS redundant
USING canonical
WHERE redundant.id = canonical.id
  AND redundant.id <> canonical.survivor_id;

ALTER TABLE images
    DROP COLUMN source_id,
    DROP COLUMN path,
    ADD CONSTRAINT uq_images_content_hash UNIQUE (content_hash);

COMMIT;
