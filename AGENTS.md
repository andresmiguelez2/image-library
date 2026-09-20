# image-library

Local image library app: indexes images from local drives, detects/embeds faces
with deepface, and provides a PySide6 UI with filtering by face, location, and
date. Metadata lives in Postgres (+pgvector) so already-processed images are
never reprocessed.

## Architecture

Three layers:

- **UI** (`src/imagelib/ui/`, PySide6): thumbnail grid + filter panel + detail/face browsers. Must never block; heavy work happens in worker threads.
- **Services** (`src/imagelib/services/`): `scanner.py` (stage-1 ingest), `catalog.py` (queries), analyzer/clustering (future).
- **Storage**: Postgres with `vector` extension (schema in `scripts/init_db.sql`, ORM in `src/imagelib/db/models.py`), original images stay on disk, thumbnails cached under `data/thumbnails`.

## The incremental pipeline (critical design rule)

Two stages; the DB `status` column drives skip-reprocessing:

1. **Ingest** (fast): walk `watched_dirs` → sha256 `content_hash` → skip unchanged → extract EXIF/GPS (`exifread`/`pillow`) → thumbnail → `status='indexed'`, visible immediately.
2. **Analyze** (slow, async): deepface detection + embeddings → `status='analyzed'`; DBSCAN clusters embeddings into `persons`.

Never reprocess an image whose `content_hash` is unchanged. Never modify original files.

DeepFace embeddings use the **Facenet** backend (512-dim) — chosen so vectors fit
pgvector's 2000-dim HNSW index ceiling (VGG-Face's 4096 would be unindexable).
The model is loaded once and reused; loading it per image is seconds each time.

## Dev commands

- Install/sync deps: `uv sync --all-groups` (Python 3.10 via `uv`; venv in `.venv`, lockfile `uv.lock`).
- Run a script: `uv run <script>` or `uv run python -m help`.
- Postgres (containerized, `pgvector/pgvector:pg16`): `docker compose up -d`, wait for healthy, DSN `postgresql+psycopg://imagelib:imagelib@localhost:5432/imagelib`.
  - First boot auto-applies `scripts/init_db.sql` (creates `vector` ext + HNSW index). To re-apply: `docker compose down -v && docker compose up -d`.
- Headless ingest CLI: `uv run imagelib-scan` (stub until scanner is wired).
- Tests: `uv run pytest -q`.

Shorthand commands are also available as opencode slash commands: `/sync`, `/db-up`, `/db-init`, `/scan`, `/test` (see `.opencode/command/`).

## Environment caveats

- **UI on headless boxes needs Qt system libs** — `sudo apt install libegl1` (desktop systems already ship it). CI/headless: set `QT_QPA_PLATFORM=offscreen`.
- Docker requires Docker Desktop → Settings → Resources → WSL integration enabled for this distro before `docker compose` works.
- User secrets live in `config/config.toml` (gitignored); `config/config.example.toml` is the template.

## Conventions

- SQLAlchemy 2.x typed ORM style (`Mapped[...]`, `mapped_column`), matching `scripts/init_db.sql` exactly — keep both in sync when changing schema.
- Config read only through `imagelib.config.config` (module-level dict).
- Do not add code comments unless asked.