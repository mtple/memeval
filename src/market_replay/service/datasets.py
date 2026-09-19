"""Fetch verified private metadata; historical events remain in indexed chunks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..datasets.chunks import ChunkedTape
from ..datasets.pack import Pack, PackError, compute_pack_id
from .storage import SupabaseObjects, checked_key


def load_stored_pack(descriptor: dict, root: Path, objects: SupabaseObjects) -> Pack:
    if descriptor.get("schema") != "market_replay_stored_pack_v1":
        raise PackError("unsupported storage descriptor")
    prefix = checked_key(descriptor["prefix"])
    pack_id = checked_key(descriptor["pack_id"])
    if "/" in pack_id or not pack_id.startswith("pack_"):
        raise PackError("invalid stored dataset identity")
    destination = root / pack_id
    index_path = objects.materialize(prefix + "/index.json", descriptor["index_sha256"], destination, "index.json")
    index = json.loads(index_path.read_bytes())
    if index["pack_id"] != descriptor["pack_id"]:
        raise PackError("storage index identifies a different dataset")
    for name, item in index["metadata"].items():
        objects.materialize(prefix + "/" + item["key"], item["sha256"], destination, "metadata/" + checked_key(name))
    pack = Pack.load(destination / "metadata", verify_hashes=False, load_tape=False)
    hashes = {o.filename: o.sha256 for o in pack.manifest.data.objects}
    if pack.pack_id != descriptor["pack_id"] or compute_pack_id(hashes, pack.params.content_hash()) != pack.pack_id:
        raise PackError("stored dataset identity does not match its original manifest")
    if any(index["source_hashes"].get(name) != digest for name, digest in hashes.items()):
        raise PackError("stored dataset provenance differs from the original manifest")
    if not pack.validation or pack.validation.get("resulting_qualification") not in ("demo", "research", "qualified_for_named_suite"):
        raise PackError("stored dataset has no successful offline validation")
    pack.event_source = ChunkedTape(index, destination, pack.manifest.period.start_utc_ms)
    pack.cl_init_ms = index["cl_init_ms"]
    pack.storage_descriptor = {**descriptor, "local_root": str(destination)}
    return pack


def publish_pack_chunks(directory: Path, objects: SupabaseObjects, *, max_bytes: int = 750_000_000) -> dict:
    """Upload immutable objects first; return the small catalog entry only when complete."""
    index_path = directory / "index.json"
    index_bytes = index_path.read_bytes()
    index = json.loads(index_bytes)
    prefix = "datasets/" + checked_key(index["pack_id"])
    items = [*index["metadata"].values(), *index["chunks"],
             {"key": "index.json", "sha256": hashlib.sha256(index_bytes).hexdigest(), "bytes": len(index_bytes)}]
    if sum(item["bytes"] for item in items) > max_bytes:
        raise PackError("dataset exceeds the explicit storage budget; do not upgrade or reduce detail automatically")
    objects.ensure_private_bucket(create=True)
    for item in items:
        data = (directory / checked_key(item["key"])).read_bytes()
        objects.put_immutable(prefix + "/" + item["key"], data, item["sha256"])
    return {"schema": "market_replay_stored_pack_v1", "pack_id": index["pack_id"], "prefix": prefix,
            "index_sha256": hashlib.sha256(index_bytes).hexdigest()}
