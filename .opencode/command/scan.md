---
description: Run the headless ingest pipeline and summarize results.
agent: image-analyser
---

Run stage-1 ingest:

```
uv run imagelib-scan --scan
```

Then report a short summary: configured source dirs, images indexed vs skipped
(unchanged hash), thumbnails written, and rows whose status flipped to
`indexed`. If the scanner still raises NotImplementedError, say so and stop —
the pipeline isn't wired yet.