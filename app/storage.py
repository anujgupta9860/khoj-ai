"""Original-file storage with a deployer-chosen backend.

Khoj keeps the original of every upload (source of truth for
re-ingestion and for the chat agent's full-text handoff). Where those
originals live is a deployment choice, set by env:

  KHOJ_STORAGE=local   (default) files on disk under KHOJ_FILES_DIR
                       (default <KHOJ_DATA_DIR>/files). Zero cloud
                       dependency — the docker-run friendly mode.
  KHOJ_STORAGE=gcs     files in the Google Cloud Storage bucket named
                       by KHOJ_BUCKET. Object keys are identical to the
                       local layout ({scope}/{filename}), so a bucket
                       populated by earlier versions stays readable.

Keys are always "{scope}/{filename}" with scope = "users/<uid>" or
"orgs/<orgId>" — the web service's upload contract; callers never pass
absolute paths.
"""

import os

from . import auth


class LocalStorage:
    backend = "local"

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, key: str) -> str:
        # Keys are server-built from verified scope + basename; still,
        # refuse anything that could escape the root.
        parts = [p for p in key.split("/") if p not in ("", ".", "..")]
        return os.path.join(self.root, *parts)

    def put(self, key: str, data: bytes, mime: str = "") -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)

    def get(self, key: str) -> bytes | None:
        try:
            with open(self._path(key), "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except OSError:
            pass


class GcsStorage:
    backend = "gcs"

    def __init__(self, bucket_name: str):
        # Imported lazily: local-only deployments never touch Google
        # libraries at all.
        from google.cloud import storage

        self._bucket = storage.Client().bucket(bucket_name)

    def put(self, key: str, data: bytes, mime: str = "") -> None:
        self._bucket.blob(key).upload_from_string(data, content_type=mime or None)

    def get(self, key: str) -> bytes | None:
        blob = self._bucket.blob(key)
        try:
            if not blob.exists():
                return None
            return blob.download_as_bytes()
        except Exception:
            return None

    def delete(self, key: str) -> None:
        try:
            blob = self._bucket.blob(key)
            if blob.exists():
                blob.delete()
        except Exception:
            pass


_storage = None


def get_storage():
    """The process-wide storage backend, chosen by KHOJ_STORAGE."""
    global _storage
    if _storage is None:
        backend = os.environ.get("KHOJ_STORAGE", "local").strip().lower()
        if backend == "gcs":
            bucket = os.environ.get("KHOJ_BUCKET", "")
            if not bucket:
                raise RuntimeError("KHOJ_STORAGE=gcs requires KHOJ_BUCKET")
            _storage = GcsStorage(bucket)
        elif backend == "local":
            root = os.environ.get(
                "KHOJ_FILES_DIR", os.path.join(auth.data_dir(), "files")
            )
            _storage = LocalStorage(root)
        else:
            raise RuntimeError(f"Unknown KHOJ_STORAGE backend: {backend!r}")
    return _storage
