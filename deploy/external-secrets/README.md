# Secrets — External Secrets Operator + Google Secret Manager

**Rule: no credential ever lives in a Helm values file, a chart, or git.**
Every service chart ships an `ExternalSecret` that syncs its credentials
from Google Secret Manager (store: `clustersecretstore.yaml`) into a
cluster-local Secret at runtime.

## One-time setup

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace
kubectl apply -f clustersecretstore.yaml
```

Bind ESO's service account (Workload Identity) to a Google service account
with `roles/secretmanager.secretAccessor` on the secrets below.

## Secrets to create in Secret Manager

| GSM secret name | Used by | Becomes env var |
| --- | --- | --- |
| `openrag-openai-api-key` | openrag-backend | `OPENAI_API_KEY` |
| `openrag-opensearch-admin-password` | openrag-backend, openrag-opensearch | `OPENSEARCH_PASSWORD` / `OPENSEARCH_INITIAL_ADMIN_PASSWORD` |
| `openrag-langflow-superuser-password` | openrag-langflow, openrag-backend | `LANGFLOW_SUPERUSER_PASSWORD` |

Create them interactively (values never touch shell history or git):

```bash
printf %s "$VALUE" | gcloud secrets create openrag-openai-api-key --data-file=- --project innovation-lab-2026
# rotate later with: gcloud secrets versions add <name> --data-file=-
```

The Khoj web service itself holds **no** credentials: it uses Workload
Identity for Vertex AI Search / GCS / Firebase Admin. The Firebase web
config in its values is a public identifier set, not a secret.
