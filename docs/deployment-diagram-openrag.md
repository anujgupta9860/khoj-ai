# Khoj — Deployment Diagram (OpenRAG on GKE)

*Nov 3, 2026 · GCP project `innovation-lab-2026` · identity/authentication intentionally omitted per request — this diagram covers the OpenRAG deployment and how Khoj consumes it.*

## Diagram

![Deployment diagram](diagrams/deployment-diagram.png)

```mermaid
flowchart TB
    subgraph Internet["Public endpoints (HTTP load balancers)"]
        KHOJIP["Khoj UI + API<br/>34.8.195.76"]
        ORIP["OpenRAG UI<br/>34.102.202.103"]
        LFIP["Langflow<br/>136.68.156.112"]
        OSIP["OpenSearch Dashboards<br/>8.233.7.242"]
    end

    subgraph GKE["GKE Autopilot cluster 'khoj' — us-central1 (regional), Kubernetes 1.36"]
        subgraph Services["Helm releases (namespace: default)"]
            WEB["khoj<br/>web + API + MCP server"]
            AGENT["khoj-agent<br/>chat / A2A service<br/>cluster-internal only"]
            ORFE["openrag-frontend<br/>Next.js UI + /api proxy"]
            ORBE["openrag-backend :8000<br/>search · files · ingest · tasks<br/>knowledge filters"]
            LF["langflow<br/>agent + ingestion flows"]
            DOC["docling :5001<br/>document parsing + OCR<br/>cluster-internal only"]
            OS["opensearch 3.8<br/>custom image with jvector<br/>'documents' index"]
            OSD["opensearch-dashboards 3.0"]
        end
        subgraph State["Persistent state (PVCs)"]
            PV1["openrag-backend state<br/>/app/keys · /app/config · /app/data<br/>(openrag.db workspace config)"]
            PV2["opensearch data volume"]
            PV3["langflow data volume"]
        end
        ESO["External Secrets Operator 2.14<br/>ClusterSecretStore 'gcpsm'"]
    end

    subgraph Platform["GCP platform services"]
        GSM[("Google Secret Manager<br/>openai key · opensearch admin pw<br/>langflow superuser pw · session secret")]
        GCS[("GCS bucket<br/>khoj-ai-corpus-innovation-lab<br/>original uploaded files")]
        FS[("SQLite<br/>users · orgs · uploads · logs")]
        AR[("Artifact Registry<br/>container images")]
        CB["Cloud Build<br/>cloudbuild.yaml pipeline"]
    end

    OAI["OpenAI API<br/>gpt-4o-mini (chat)<br/>text-embedding-3-small (embeddings)"]

    KHOJIP --> WEB
    ORIP --> ORFE
    LFIP --> LF
    OSIP --> OSD

    WEB -->|"A2A message/send (in-cluster)"| AGENT
    WEB -->|"upload_ingest · search · files<br/>http://openrag-backend:8000"| ORBE
    AGENT -->|"search · tool-loop searches<br/>http://openrag-backend:8000"| ORBE
    WEB -->|"store / read originals"| GCS
    AGENT -->|"full-text fetch of originals"| GCS
    WEB --> FS
    AGENT --> FS

    ORFE -->|"proxies /api/* → backend (prefix stripped)"| ORBE
    ORBE --> OS
    ORBE --> LF
    ORBE --> DOC
    LF --> DOC
    OSD --> OS

    ORBE --- PV1
    OS --- PV2
    LF --- PV3

    ESO -->|"syncs secrets into pods"| WEB
    ESO -->|"syncs secrets into pods"| AGENT
    ESO -->|"syncs secrets into pods"| ORBE
    ESO -->|"syncs secrets into pods"| LF
    GSM --> ESO

    ORBE -->|"embeddings + OpenRAG chat LLM"| OAI
    AGENT -->|"answer model"| OAI

    CB -->|"build + push images"| AR
    CB -->|"helm upgrade"| GKE
    AR -.->|"image pulls"| GKE
```

## Reading the diagram

- **One cluster, two products.** GKE Autopilot runs the Khoj services (`khoj`, `khoj-agent`)
  and the full OpenRAG stack side by side as separate Helm releases in the same namespace.
  Khoj treats OpenRAG as an internal service: all Khoj→OpenRAG traffic stays in-cluster at
  `http://openrag-backend:8000`.
- **Route contract (learned the hard way).** The OpenRAG backend serves **unprefixed** routes
  (`/search`, `/files`, `/router/upload_ingest`, `/tasks/...`, `/knowledge-filter`). The
  `/api/*` prefix exists only on the frontend's proxy, which strips it. Anything calling the
  backend with `/api/*` gets a 404.
- **OpenRAG internals.** The backend orchestrates: Docling parses documents (OCR on),
  Langflow runs the ingestion and agent flows, OpenSearch (custom 3.8 image — the stock
  image lacks the jvector engine the index requires) holds the `documents` index. Backend
  state that upstream keeps on local disk (keys, config, `openrag.db`) lives on PVCs so pod
  restarts don't wipe configuration.
- **Kubernetes gotcha baked into the charts.** Every workload sets
  `enableServiceLinks: false`. With service links on, Kubernetes injects
  `OPENRAG_BACKEND_PORT`-style variables that collide with the apps' own configuration
  (this caused both a broken OIDC discovery URL and search 401s during bring-up).
- **Secrets.** Nothing sensitive is in Helm values or git. External Secrets Operator syncs
  from Google Secret Manager (see the production deployment guide for the full setup).
- **Delivery.** Cloud Build builds the Khoj images (and the patched OpenRAG backend image),
  pushes them to Artifact Registry in-step, then runs `helm upgrade` for both Khoj releases
  using a static-token kubeconfig (the stock helm image has no `gke-gcloud-auth-plugin`).
  Deploys are currently submitted manually; wiring the GitHub trigger needs one console
  OAuth step.
- **Exposure.** Four public HTTP load-balancer IPs (Khoj, OpenRAG UI, Langflow, Dashboards).
  The agent and Docling are cluster-internal only. Production hardening (domain + HTTPS)
  is covered in the deployment guide.
