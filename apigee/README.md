# Apigee — khoj-api proxy

Public front door for Khoj.ai: browser/app traffic hits Apigee at
`/khoj/v1`, which adds CORS headers and spike arrest (100 req/min), then
forwards to the khoj service behind its GKE ingress. Authentication is
unchanged — the service verifies Firebase ID tokens itself; add a
VerifyAPIKey policy later only if third-party API access is productized.

Import: `zip -r khoj-api.zip apiproxy` from inside `apigee/khoj-api/`,
then Apigee console → Develop → API Proxies → Import proxy bundle.
After the GKE ingress exists, set the real backend URL in
`apiproxy/targets/default.xml` and upload a new revision.
