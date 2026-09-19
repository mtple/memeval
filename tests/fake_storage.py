"""In-memory immutable private objects for offline service tests."""
import hashlib
from pathlib import Path

from market_replay.service.storage import StorageError, checked_key


class PrivateObjects:
    def __init__(self):
        self.data = {}
        self.downloads = []
        self.uploads = []
        self.fail_upload = False

    def ensure_private_bucket(self, *, create=False):
        pass

    def put_immutable(self, key, data, sha256):
        checked_key(key)
        assert hashlib.sha256(data).hexdigest() == sha256
        if self.fail_upload and key.startswith("observations/"):
            raise StorageError("injected private upload failure")
        if key in self.data:
            assert self.data[key] == data
        self.data[key] = data
        self.uploads.append(key)

    def materialize(self, key, sha256, root, relative):
        checked_key(key)
        target = Path(root) / checked_key(relative)
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == sha256:
            return target
        data = self.data[key]
        assert hashlib.sha256(data).hexdigest() == sha256
        self.downloads.append(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def close(self):
        pass

