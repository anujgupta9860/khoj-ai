#!/usr/bin/env bash
# Migrate every Khoj corpus file from the GCS bucket into OpenRAG,
# under the doc-id-prefixed name the Khoj services filter on
# (<docId>--<filename>, docId = "doc-" + sha256("{scope}/{filename}")[:24]).
#
# Idempotent: OpenRAG replaces a document uploaded under the same name.
# Run from Cloud Shell (needs gsutil + curl + python3). The bucket's
# originals are NOT modified - they stay as cold copies.
set -u
BUCKET="${KHOJ_BUCKET:-khoj-ai-corpus-innovation-lab}"
OPENRAG="${OPENRAG_URL_PUBLIC:-http://34.102.202.103}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

doc_id() {
  python3 -c 'import hashlib,sys; print("doc-" + hashlib.sha256(sys.argv[1].encode()).hexdigest()[:24])' "$1"
}

count=0
fail=0
while IFS= read -r uri; do
  case "$uri" in
    gs://*/users/*|gs://*/orgs/*) ;;
    *) continue ;;
  esac
  rel="${uri#gs://$BUCKET/}"
  scope="$(echo "$rel" | cut -d/ -f1-2)"
  filename="$(basename "$rel")"
  [ -z "$filename" ] && continue
  id="$(doc_id "$scope/$filename")"
  or_name="${id}--${filename}"
  local_file="$TMP/file"
  if ! gsutil -q cp "$uri" "$local_file"; then
    echo "DOWNLOAD FAILED: $uri"; fail=$((fail+1)); continue
  fi
  code="$(curl -s -o "$TMP/resp" -w '%{http_code}' -F "file=@$local_file;filename=$or_name" "$OPENRAG/api/router/upload_ingest")"
  if [ "$code" = "202" ] || [ "$code" = "200" ]; then
    echo "OK  $rel -> $or_name ($(head -c 120 "$TMP/resp"))"
    count=$((count+1))
  else
    echo "INGEST FAILED ($code): $rel"; fail=$((fail+1))
  fi
  rm -f "$local_file"
done < <(gsutil ls -r "gs://$BUCKET/users/**" "gs://$BUCKET/orgs/**" 2>/dev/null | grep -v ':$')

echo "MIGRATED: $count  FAILED: $fail"
