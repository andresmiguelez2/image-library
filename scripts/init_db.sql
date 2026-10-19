-- image-library schema bootstrap.
-- Run automatically on first container start, or manually:
--   psql -U imagelib -d imagelib -f scripts/init_db.sql

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS sources (
    id          SERIAL PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    label       TEXT,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS images (
    id            SERIAL PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    size_bytes    BIGINT,
    width         INTEGER,
    height        INTEGER,
    taken_at      TIMESTAMPTZ,
    modified_at   TIMESTAMPTZ,
    gps_lat       DOUBLE PRECISION,
    gps_lon       DOUBLE PRECISION,
    gps_place     TEXT,
    camera_make   TEXT,
    camera_model  TEXT,
    thumb_path    TEXT,
    face_count    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'indexed', 'analysed', 'error')),
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_images_content_hash UNIQUE (content_hash)
);

CREATE TABLE IF NOT EXISTS image_locations (
    id         SERIAL PRIMARY KEY,
    image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    CONSTRAINT uq_image_locations_path UNIQUE (path)
);

CREATE INDEX IF NOT EXISTS ix_image_locations_image_id ON image_locations (image_id);
CREATE INDEX IF NOT EXISTS ix_image_locations_source_id ON image_locations (source_id);

CREATE TABLE IF NOT EXISTS persons (
    id           SERIAL PRIMARY KEY,
    name         TEXT,
    embedding    vector(512),
    cover_face_id INTEGER,
    CONSTRAINT uq_persons_name UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS faces (
    id         SERIAL PRIMARY KEY,
    image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    person_id  INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    w          REAL NOT NULL,
    h          REAL NOT NULL,
    embedding  vector(512),
    confidence REAL
);

-- Match state refers to stable face IDs and named-person IDs, not transient
-- unnamed DBSCAN cluster rows. Rebuilding clusters therefore preserves it.
CREATE TABLE IF NOT EXISTS face_match_decisions (
    face_id         INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    target_person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE RESTRICT,
    status          TEXT NOT NULL DEFAULT 'proposed',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (face_id, target_person_id),
    CONSTRAINT ck_face_match_decisions_status
        CHECK (status IN ('proposed', 'accepted', 'rejected'))
);

-- A face can be accepted for at most one named identity; any number of
-- proposals/rejections for other identities can remain available for review.
CREATE UNIQUE INDEX IF NOT EXISTS uq_face_match_decisions_one_accepted_per_face
    ON face_match_decisions (face_id)
    WHERE status = 'accepted';

-- Similarity search for face matching (cosine distance).
CREATE INDEX IF NOT EXISTS idx_faces_embedding
    ON faces USING hnsw (embedding vector_cosine_ops);

CREATE OR REPLACE FUNCTION public.clear_catalog()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $clear_catalog$
DECLARE
    required_table TEXT;
    table_name TEXT;
    sequence_name TEXT;
BEGIN
    FOREACH required_table IN ARRAY ARRAY[
        'sources',
        'images',
        'faces',
        'persons',
        'face_match_decisions'
    ] LOOP
        IF to_regclass(format('public.%I', required_table)) IS NULL THEN
            RAISE EXCEPTION
                'Cannot clear catalogue: required table public.% is missing',
                required_table;
        END IF;
    END LOOP;

    LOCK TABLE public.sources,
        public.images,
        public.faces,
        public.persons,
        public.face_match_decisions
        IN ACCESS EXCLUSIVE MODE;

    IF to_regclass('public.image_locations') IS NOT NULL THEN
        EXECUTE 'LOCK TABLE public.image_locations IN ACCESS EXCLUSIVE MODE';
        EXECUTE 'DELETE FROM public.image_locations';
    END IF;

    DELETE FROM public.face_match_decisions;
    DELETE FROM public.persons;
    DELETE FROM public.faces;
    DELETE FROM public.images;
    DELETE FROM public.sources;

    FOREACH table_name IN ARRAY ARRAY['sources', 'images', 'faces', 'persons'] LOOP
        sequence_name := pg_get_serial_sequence(format('public.%I', table_name), 'id');
        IF sequence_name IS NOT NULL THEN
            EXECUTE format('ALTER SEQUENCE %s RESTART WITH 1', sequence_name::regclass);
        END IF;
    END LOOP;

    IF to_regclass('public.image_locations') IS NOT NULL THEN
        sequence_name := pg_get_serial_sequence('public.image_locations', 'id');
        IF sequence_name IS NOT NULL THEN
            EXECUTE format('ALTER SEQUENCE %s RESTART WITH 1', sequence_name::regclass);
        END IF;
    END IF;
END;
$clear_catalog$;
