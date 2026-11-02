# Build & deploy pipeline (Cloud Build)

`cloudbuild.yaml` at the repo root:

1. Builds the khoj web image and the patched openrag-backend image.
2. Pushes both to Artifact Registry `us-central1-docker.pkg.dev/innovation-lab-2026/khoj`
   (tags: `$SHORT_SHA` and `latest`).
3. Fetches credentials for the GKE Autopilot cluster `khoj` (us-central1)
   and runs `helm upgrade --install khoj ./charts/khoj` with the new tag.

Trigger: Cloud Build → Triggers → connect this GitHub repo → trigger on
push to `main`. Required IAM for the Cloud Build service account
(`<project-number>@cloudbuild.gserviceaccount.com`):
`roles/artifactregistry.writer`, `roles/container.developer`.

OpenRAG phase-2 charts deploy manually for now
(`helm upgrade --install <svc> ./charts/openrag-<svc>`), since they're
evaluation infrastructure, not the product surface.
