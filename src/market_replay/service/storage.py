"""Private immutable objects for hosted datasets and checkpoints.

Only the service/collection layer uses this client. No URL, key, or storage path is
returned through the agent plane. Existing DATABASE_URL remains the run store.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse

import httpx


class StorageError(RuntimeError):
    pass


def checked_key(key: str) -> str:
    p = PurePosixPath(key)
    if not key or key == "." or p.is_absolute() or ".." in p.parts or str(p) != key or "\\" in key:
        raise StorageError("invalid private object key")
    return key


class SupabaseObjects:
    def __init__(self, url: str, secret: str, *, bucket: str = "market-replay-private", transport=None):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
            raise StorageError("SUPABASE_URL must be an HTTPS project origin")
        if not secret:
            raise StorageError("Supabase Storage requires a server-only secret key")
        self.bucket = checked_key(bucket)
        if "/" in self.bucket:
            raise StorageError("invalid private bucket")
        self.client = httpx.Client(base_url=url.rstrip("/") + "/storage/v1", headers={"apikey": secret, "Authorization": f"Bearer {secret}"},
                                   timeout=httpx.Timeout(25, connect=5), transport=transport)

    @classmethod
    def from_env(cls):
        url = os.environ.get("SUPABASE_URL", "")
        secret = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SECRET_KEY", "")
        if not url or not secret:
            raise StorageError("Configure SUPABASE_URL and a server-only SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SECRET_KEY; database credentials alone do not authorize Storage")
        return cls(url, secret, bucket=os.environ.get("MARKET_REPLAY_STORAGE_BUCKET", "market-replay-private"))

    def ensure_private_bucket(self, *, create: bool = False) -> None:
        response = self.client.get(f"/bucket/{self.bucket}")
        if response.status_code == 404 and create:
            response = self.client.post("/bucket", json={"id": self.bucket, "name": self.bucket, "public": False, "file_size_limit": 50 * 1024 * 1024})
            if response.status_code not in (200, 201, 409):
                raise StorageError(f"private bucket creation failed (HTTP {response.status_code})")
            response = self.client.get(f"/bucket/{self.bucket}")
        if response.status_code != 200:
            raise StorageError(f"private bucket unavailable (HTTP {response.status_code})")
        if response.json().get("public") is not False:
            raise StorageError("historical data requires a private bucket; refusing public storage")

    def get(self, key: str, expected_sha256: str) -> bytes:
        response = self.client.get("/object/authenticated/" + self.bucket + "/" + quote(checked_key(key), safe="/"))
        if response.status_code != 200:
            raise StorageError(f"private object download failed (HTTP {response.status_code})")
        data = response.content
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise StorageError("private object hash mismatch")
        return data

    def put_immutable(self, key: str, data: bytes, sha256: str) -> None:
        if hashlib.sha256(data).hexdigest() != sha256:
            raise StorageError("refusing upload with mismatched content hash")
        if len(data) > 50 * 1024 * 1024:
            raise StorageError("object exceeds the configured 50 MiB limit; split it into indexed chunks")
        response = self.client.post("/object/" + self.bucket + "/" + quote(checked_key(key), safe="/"), content=data,
                                    headers={"Content-Type": "application/octet-stream", "x-upsert": "false"})
        if response.status_code in (400, 409):
            # Do not overwrite. A retry is successful only if the existing bytes match.
            self.get(key, sha256)
        elif response.status_code not in (200, 201):
            raise StorageError(f"private object upload failed (HTTP {response.status_code})")

    def materialize(self, key: str, sha256: str, root: Path, relative: str) -> Path:
        target = root / checked_key(relative)
        if not target.resolve().is_relative_to(root.resolve()):
            raise StorageError("private object escapes its local cache")
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == sha256:
            return target
        data = self.get(key, sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Concurrent workers may write identical immutable objects. Atomic replace
        # prevents a reader from ever opening a partially downloaded chunk.
        import tempfile
        fd, name = tempfile.mkstemp(dir=target.parent, prefix=".download-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)
        return target

    def close(self):
        self.client.close()
