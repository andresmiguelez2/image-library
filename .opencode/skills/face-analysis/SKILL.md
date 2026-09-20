---
name: face-analysis
description: Use when implementing or debugging face detection/embedding with deepface, person clustering, or the images.status incremental pipeline in image-library. Do not use for general image metadata/EXIF work.
---

# DeepFace pipeline

Two stages, gated by the `images.status` column (`pending` → `indexed` →
`analyzed`/`error`) and the `content_hash` uniqueness check.

## Stage 2 (analyze)

Per image: detect faces → embed each → store `faces` rows → cluster into
`persons` → `status='analyzed'`.

```python
from deepface import DeepFace

faces = DeepFace.extract_faces(img_path=path, detector_backend="retinaface")
# embeddings are best computed via DeepFace.represent with the model once loaded
```

## Backend choice — non-negotiable

Use **Facenet** (512-dim). VGG-Face is 4096-dim and **cannot** be indexed by
pgvector's HNSW/IVFFlat (2000-dim ceiling), so similarity search would degrade
to linear scans. Facenet vectors + HNSW `vector_cosine_ops` is the design
constraint.

## Performance

- Load the model ONCE per process and reuse it. Warm-up is seconds; per-image
  loads would dominate runtime.
- Work in batches/worker threads; the UI must never block on analysis.
- Heavy stacks: TensorFlow on CPU is acceptable for background jobs; don't add
  GPU orchestration without being asked.

## Clustering

Group embeddings with `sklearn.cluster.DBSCAN` (eps/min_samples in
`config/config.example.toml` under `[analysis]`). Write one `persons` row per
cluster (centroid = mean embedding), link `faces.person_id`. Threshold config
supports dedupe/identity via cosine similarity.

## Incremental rule

Never recompute for an image whose `content_hash` is unchanged and whose status
is already terminal. If analysis fails midway, set `status='error'` with the
message and let the next run retry only if the file changed or a force flag is
passed.