# Khoj.ai — SDR and ADR for the POC

*System Design Review + Architecture Decision Records · Nov 3, 2026 · Status: POC live at http://34.8.195.76 (GCP project `innovation-lab-2026`, GKE Autopilot).*

---

# Part 1 — System Design Review (SDR)

## 1. Problem statement

Build a hosted document-search product where individuals and small businesses upload their
own documents and can **search and chat over exactly their own corpus** — never anyone
else's — with an org layer (a business corpus shared by its members), a super-admin view for
the platform operator, and machine access (MCP + A2A) for agents. The POC also serves as the
evaluation vehicle for OpenRAG as the retrieval engine behind a skill-based agent stack.

## 2. Goals and non-goals

**Goals**
- Per-user corpora and per-org corpora with hard isolation, enforced server-side.
- Upload → parse → index → search → chat, end to end, on one retrieval engine (OpenRAG).
- Chat quality comparable to OpenRAG's own UI on the same documents.
- Org administration: enroll, invite, roles (admin/member), branding, pending approval.
- Platform super admin: approve orgs, see all conversations and RAG metrics.
- MCP tools and an A2A agent so other agents can search a user's corpus with their token.
- No secrets in code, charts, or git — everything via External Secrets Operator.

**Non-goals (POC)**
- Production identity hardening, domains/HTTPS, email delivery for invites.
- Billing, quotas, multi-region, HA beyond what GKE Autopilot gives by default.
- Replacing OpenRAG's own UI — it remains an operator/evaluation surface.

## 3. Architecture overview

```
Clients (browser, MCP client, A2A client)
        │
   Khoj web (FastAPI: UI + REST + MCP)  ──► Firestore (orgs, uploads, logs)
        │ A2A message/send                     GCS (original files)
        ▼
   Khoj agent (A2A + chat) ──► OpenAI gpt-4o-mini (answer model + tool loop)
        │ scoped search (filename filter + post-filter)
        ▼
   OpenRAG backend ──► OpenSearch (documents index) · Langflow (flows) · Docling (parse/OCR)
        ▲
   Khoj web ── upload_ingest (scoped filename), knowledge filters
```

One GKE Autopilot cluster hosts both products as separate Helm releases. Khoj calls the
OpenRAG backend in-cluster on its **unprefixed** routes (`/search`, `/files`,
`/router/upload_ingest`, `/knowledge-filter`); the `/api/*` prefix exists only on OpenRAG's
frontend proxy. Full diagrams: `khoj-deployment-diagram-openrag.md`,
`khoj-chat-isolation-flow.md`.

## 4. Key design points

**4.1 Corpus isolation by naming convention.** Files are ingested as
`{docId}--{filename}` with `docId = doc- + sha256(scope/filename)[:24]`, scope =
`users/{uid}` or `orgs/{orgId}`. Every chat/search builds the caller's allowed-name set
(own scope + platform-org "global" corpus) from Firestore upload records, sends it as a
server-side OpenRAG filename filter, and post-filters returned chunks against it. Detail and
live verification: chat isolation document.

**4.2 Chat pipeline.** Query rewrite (model) + raw query → merged scoped retrieval →
gpt-4o-mini (the same model OpenRAG's chat is configured with) in an OpenRAG-style **tool
loop**: seeded excerpts first, up to 3 `search_documents` tool rounds (same filters enforced
inside the tool), one refusal-retry. Small documents (≤20k chars) are handed over as full
text — form-like PDFs chunk into glossaries that outrank filled values, which made
chunk-only chat refuse on a real W-2 whose chunks provably contained the data. Extractive
fallback (quote full sections of the caller's source files) covers model outages.

**4.3 Relevance floor.** OpenRAG's kNN returns nearest chunks for *any* query on small
corpora; measured junk baseline is score 1.25–1.29 on every corpus tried, genuine matches
≥1.65. Web search drops chunks below `KHOJ_MIN_SCORE` (default 1.5) and drops empty-text
"shell hits", so unrelated queries return zero results instead of the caller's documents.

**4.4 Knowledge tab.** OpenRAG's Knowledge tab lists *filters*, not files; upstream's own
convention is one filter per uploaded document (`data_sources = [filename]`). Khoj registers
such a filter on every upload, so Khoj documents land in the OpenRAG Knowledge tab too.

**4.5 Data stores.** Firestore: orgs, memberships, invites, `file_uploads`, `search_logs`
(every search/upload/chat, powering the super-admin panel and metrics). GCS bucket:
original files under `users/{uid}/…` / `orgs/{orgId}/…` (cold copy + full-text fetch source).
OpenSearch: the single `documents` index. OpenRAG backend state (incl. `openrag.db`
workspace config) on PVCs.

**4.6 Secrets.** GSM → External Secrets Operator (ClusterSecretStore `gcpsm`) → Kubernetes
secrets consumed by both products. The OpenAI key is shared (GSM `openrag-openai-api-key`).
Firebase web config values in charts are public identifiers, not secrets (a GitHub
secret-scanning alert on the apiKey was a false positive — it is the standard public web key).

## 5. Verification performed (Nov 3, 2026)

- Clean-slate rebuild: bucket emptied, Firestore upload records wiped, OpenRAG index emptied,
  Vertex engine + data store deleted (GET → 404), OCR enabled, both products redeployed.
- Personal E2E: upload → ready → scoped search hit → chat answer with citations; a second
  user saw none of it.
- Org E2E (two orgs + global doc): isolation held across search, document list, and chat;
  cross-org API access 403; global document visible to both orgs by design.
- Chat parity tests: form-layout fixture (glossary-first, labels/values separated) — chat
  now answers with the document's real figures; plain-document regression unchanged;
  isolation re-checked after the tool loop shipped.
- Failure-driven fixes, each reproduced first: `/api/*` 404 path bug, Firestore
  collection-group index 500s, service-link env collisions, missing jvector in the stock
  OpenSearch image, onboarding wizard state quirk (API onboarding leaves `current_step=0`).

## 6. Risks and known limitations

| Risk / limitation | Mitigation / path |
|---|---|
| Isolation is a Khoj-layer property; OpenRAG's own UI searches the whole index | Treat OpenRAG UI as operator-only; never expose it to tenants |
| All endpoints are HTTP on bare IPs | Domain + HTTPS before any real users (deployment guide) |
| Chat depends on an external model API (OpenAI) | Extractive fallback keeps chat functional; key is centralized in GSM |
| Score floor is calibrated on today's corpora; BM25 component shifts scores with corpus size | `KHOJ_MIN_SCORE` is env-tunable; re-measure when corpora grow |
| Form pairing on adversarial layouts can be off by a slot (synthetic fixture) | Full-text handoff + tool loop minimized it; real W-2 layout interleaves labels/values — acceptance test is the owner's own retest |
| Deploys are manual Cloud Build submits | GitHub trigger pending one console OAuth step |
| Invite emails have no provider yet | In-app matching + one-time links work today |

---

# Part 2 — Architecture Decision Records (ADR)

## ADR-001: One retrieval engine — OpenRAG; delete Vertex AI Search
**Status:** Accepted, Nov 3, 2026.
**Context:** Khoj started on Vertex AI Search; OpenRAG was deployed alongside for the POC
evaluation, giving two indexes, two ingestion paths, and diverging answers.
**Decision:** Make GKE OpenRAG the single retrieval/indexing engine. Delete the Vertex
engine and data store and all Discovery Engine code from web and agent.
**Consequences:** One ingestion path, one index, answers comparable 1:1 with the OpenRAG UI.
Khoj inherits OpenRAG's constraints (filename-keyed filtering, filter-based Knowledge tab)
and its operational quirks, documented in the deployment guide.

## ADR-002: Keep the Khoj product shell; swap only the engine
**Context:** The alternative was adopting OpenRAG's UI wholesale.
**Decision:** Keep Khoj's UI, orgs, invites, super-admin, MCP and A2A surfaces; replace only
retrieval/indexing underneath.
**Consequences:** Product model (per-user/per-org corpora, pending org approval) survives the
engine swap unchanged; all isolation logic lives in Khoj, where the tenancy model exists.

## ADR-003: Isolation via filename convention + dual filtering
**Context:** OpenRAG offers no per-tenant index partitioning usable here; upstream filtering
keys on filename.
**Decision:** Scope is encoded in the ingested filename (`{docId}--{filename}`, docId from a
hash of scope+filename). Retrieval uses a server-side filename filter naming exactly the
caller's documents, **plus** a post-filter in Khoj. Full-text fetches read only the owning
scope's GCS prefix.
**Consequences:** Isolation is simple, auditable, and verified end-to-end; it depends on
discipline — every retrieval path must build the allowed-name set (the chat tool loop was
explicitly re-verified against this). The OpenRAG UI itself remains unscoped (operator tool).

## ADR-004: Global corpus = platform org's documents, unioned into every search
**Context:** The operator wants some documents (e.g., handbook) searchable by all users
without copying them per tenant.
**Decision:** Documents uploaded to the platform org (`khoj`) form a global corpus that is
unioned with the caller's own scope in search, list, and chat; results are tagged
`corpus=global|own`. Other orgs' documents are never unioned.
**Consequences:** One canonical copy serves everyone; publishing to the global corpus is an
operator action with product-wide effect, so it stays admin-only.

## ADR-005: Chat = OpenRAG-style tool loop on the same model (gpt-4o-mini)
**Context:** One-shot grounded chat diverged from OpenRAG UI answers: it refused on a real
W-2 (glossary chunks outranked values), then — after full-text handoff — still failed a
form-layout fixture, once inventing a figure. OpenRAG's advantage proved to be *iteration*,
not the model (its config also uses gpt-4o-mini).
**Decision:** Khoj chat seeds the model with scoped excerpts and gives it a
`search_documents` tool (≤3 rounds) with ADR-003 filtering enforced inside the tool; a
first-pass refusal triggers one firmer re-read.
**Consequences:** Answer quality converged with the OpenRAG UI on the test corpus while
Khoj's filtering stayed exactly as-is. Latency rose modestly (~8s on multi-search answers).

## ADR-006: Full-text handoff for small documents
**Context:** Form-like PDFs (W-2, statements) chunk badly: top-ranked chunks are instruction
pages and label lists, values sit in layout-scrambled chunks.
**Decision:** When a document's extracted full text is ≤20,000 characters, the model receives
the whole text instead of (or in addition to) chunks — in seeded retrieval and in tool
results (once per document per answer).
**Consequences:** Form questions became answerable; token cost is bounded by the size cap
and the per-answer document caps.

## ADR-007: Relevance floor for web search (KHOJ_MIN_SCORE = 1.5)
**Context:** kNN returns the caller's nearest documents for unrelated queries on small
corpora (measured junk scores 1.25–1.29 vs genuine ≥1.65), making junk searches list real
documents.
**Decision:** Drop chunks below the floor (env-tunable) and drop empty-text shell hits in web
search results. Chat keeps its own behaviour (grounded refusal + tool loop).
**Consequences:** Unrelated searches return zero results; the floor must be re-measured if
corpora grow large enough to shift the score distribution.

## ADR-008: Knowledge-tab presence via per-document knowledge filters
**Context:** Khoj uploads were invisible in OpenRAG's Knowledge tab (it lists filters, not
files; local uploads belong to no filter).
**Decision:** On upload, Khoj registers an OpenRAG knowledge filter for the document
(`data_sources=[its name]`, name = original filename) — upstream's own onboarding
convention. Existing documents were backfilled.
**Consequences:** Khoj documents are browsable in both UIs; filter count grows with document
count (acceptable at POC scale; a per-scope roll-up filter is the future refinement).

## ADR-009: All secrets via External Secrets Operator from GSM
**Context:** Early bring-up risked scattering keys across values files and two products.
**Decision:** Every credential (OpenAI key, OpenSearch admin password, Langflow superuser
password, session secret) lives in Google Secret Manager and is synced by ESO 2.14
(ClusterSecretStore `gcpsm`, `external-secrets.io/v1`). Nothing secret is committed;
Firebase web config is treated as public identifiers.
**Consequences:** One rotation point per secret; deploys require the ESO plumbing described
in the production deployment guide before first install.

## ADR-010: Call the OpenRAG backend directly on unprefixed routes
**Context:** Khoj's first swap build called `/api/search` etc. in-cluster and every upload
failed with "Ingestion trigger: Not Found" — `/api/*` exists only on OpenRAG's frontend
proxy, which strips the prefix.
**Decision:** In-cluster callers use the backend's native unprefixed routes; the contract is
commented at the call sites and pinned by unit tests that stub exact paths.
**Consequences:** One less hop (no proxy dependency for machine traffic); the route contract
is now a documented integration fact for any future consumer.

## ADR-011: Clean-slate cutover instead of data migration
**Context:** Mid-swap, existing Khoj/Vertex documents and OpenRAG sample data were
intermixed; a migration path was built, then the call was made to simplify.
**Decision:** Wipe both sides (bucket objects, upload records, OpenRAG index), delete Vertex,
and re-seed from uploads — with accounts, orgs, and logins preserved.
**Consequences:** The single-stack state was reached the same day with no migration code to
maintain; users re-upload documents (uploads verified working immediately after).

## ADR-012: Keep an extractive fallback for chat
**Context:** Chat originally ran extractive-only while Gemini access was blocked at the
project/org level; the OpenAI route later became primary.
**Decision:** Retain the extractive path (fetch the caller's source file, quote the best
complete sections) as the automatic fallback when the model call fails or no key is set.
**Consequences:** Chat degrades gracefully instead of erroring; fallback answers obey the
same corpus boundary because they read only the scope's GCS objects.
