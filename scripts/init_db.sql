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
                  CHECK (status IN ('pending', 'indexed', 'analysed', 'error')),
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
