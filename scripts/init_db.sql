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
    source_id     INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    path          TEXT NOT NULL UNIQUE,
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
                  CHECK (status IN ('pending', 'indexed', 'analyzed', 'error')),
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS persons (
    id           SERIAL PRIMARY KEY,
    name         TEXT,
    embedding    vector(512),
    cover_face_id INTEGER
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

-- Similarity search for face matching (cosine distance).
CREATE INDEX IF NOT EXISTS idx_faces_embedding
    ON faces USING hnsw (embedding vector_cosine_ops);