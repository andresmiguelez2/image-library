"""image-library: local image index, face analysis, and browsing.

Layout:
- db: SQLAlchemy models + session factory (Postgres + pgvector)
- services: scanner (ingest), catalog (queries)
- ui: PySide6 frontend
"""

__version__ = "0.1.0"