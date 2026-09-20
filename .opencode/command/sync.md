---
description: Install/sync project dependencies with uv and smoke-check imports.
---

Run `uv sync --all-groups`, then verify the key imports resolve:

```
uv run python -c "import deepface, PySide6, sqlalchemy, pgvector, psycopg, exifread, PIL, sklearn"
```

If anything fails, fix the environment or pyproject before reporting. Note: on
headless boxes the PySide6 import may fail with libEGL.so.1 missing — that
needs `sudo apt install libegl1`, not a code fix.