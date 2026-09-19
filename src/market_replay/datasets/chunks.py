"""Immutable tape chunks and a bounded, local-only event sequence.

The service supplies missing files before executing engine work. This module never
fetches objects, and neither the index nor MissingChunk descriptors are public
agent payloads. Exporting preserves each normalized source row without sampling.
"""

from __future__ import annotations

import bisect
import gzip
import hashlib
import io
import json
import shutil
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, overload

from ..domain.models import TapeEvent
from ..engine.tape import RelEvent, to_relative
from .pack import Pack, PackError, iter_jsonl, sha256_file, tape_path

CHUNK_SCHEMA = "market_replay_tape_chunks_v1"
DEFAULT_MAX_EVENTS = 10_000
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
_METADATA = (
    "manifest.yaml", "assets.jsonl", "pools.jsonl", "blocks.jsonl", "restrictions.jsonl",
    "coverage.json", "validation.json", "inventory.json", "provenance.json", "provenance.yaml", "utc-boundaries.json",
)


class ChunkIntegrityError(PackError):
    """A private dataset object does not match its immutable index."""


class MissingChunk(FileNotFoundError):
    """Private signal to the service to supply a verified chunk and resume."""

    def __init__(self, descriptor: dict[str, Any], path: Path) -> None:
        self.descriptor = dict(descriptor)
        self.path = path
        super().__init__(f"required dataset chunk is not local: {descriptor['key']}")


def _member(root: Path, name: str) -> Path:
    part = PurePosixPath(name)
    if part.is_absolute() or not part.parts or ".." in part.parts or "\\" in name:
        raise ChunkIntegrityError("invalid private dataset object path")
    path = root.joinpath(*part.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ChunkIntegrityError("dataset object escapes its local root")
    return path


def _line(row: dict[str, Any]) -> bytes:
    return (json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def _order(row: dict[str, Any]) -> tuple[int, int, int]:
    return int(row["block"]), int(row["log_index"]), int(row["seq"])


def _gzip(data: bytes) -> bytes:
    # GzipFile fixes filename and timestamp, including the OS byte across platforms.
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=6) as stream:
        stream.write(data)
    return buffer.getvalue()


def export_pack_chunks(
    pack_dir: Path | str,
    out_dir: Path | str,
    *,
    max_events: int = DEFAULT_MAX_EVENTS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Validate and stream a pack into deterministic chunks and ``index.json``.

    Limits apply to both a chunk and each complete equal-time event group. A
    group that cannot fit is rejected with instructions to raise the explicit
    limits; it is never truncated or divided across requests. Original tape file
    hashes prove provenance, while normalized hashes prove every row survives
    rechunking. Only known pack metadata is copied, never collector work files.
    """
    if type(max_events) is not int or max_events < 1 or type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_events and max_bytes must be positive integers")
    source, destination = Path(pack_dir), Path(out_dir)
    if source.resolve() == destination.resolve() or destination.resolve().is_relative_to(source.resolve()):
        raise ValueError("chunk output must be outside the immutable source pack")
    # Metadata is modest; importantly, this does not load or count the tape.
    pack = Pack.load(source, load_tape=False)
    tape = tape_path(source)
    if tape is None:
        raise PackError("pack has no historical tape")
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / "index.json").exists():
        previous_index = json.loads((destination / "index.json").read_text())
        if previous_index.get("pack_id") != pack.pack_id:
            raise PackError("chunk destination already contains a different immutable pack")
    source_hashes = {obj.filename: obj.sha256 for obj in pack.manifest.data.objects}
    if tape.name not in source_hashes:
        raise PackError("selected tape is not declared in the immutable pack manifest")
    metadata: dict[str, dict[str, Any]] = {}
    names = set(_METADATA) | {pack.manifest.execution.parameters_file}
    # Declared data objects must be recognized metadata or the source tape.
    unknown = set(source_hashes) - names - {tape.name}
    if unknown:
        raise PackError(f"unsupported pack metadata files; extend the explicit metadata allowlist: {sorted(unknown)}")
    for name in sorted(names):
        original = _member(source, name)
        if not original.is_file():
            continue
        digest = sha256_file(original)
        source_hashes[name] = digest
        key = f"metadata/{name}"
        target = _member(destination, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        metadata[name] = {"key": key, "sha256": digest, "bytes": original.stat().st_size}

    descriptors: list[dict[str, Any]] = []
    canonical_digest = hashlib.sha256()
    cl_init_ms: dict[str, int] = {}
    chunk_rows: list[dict[str, Any]] = []
    chunk_lines: list[bytes] = []
    chunk_bytes = 0
    group_rows: list[dict[str, Any]] = []
    group_lines: list[bytes] = []
    group_bytes = 0
    previous_order: tuple[int, int, int] | None = None
    previous_time: int | None = None
    total = 0

    def emit_chunk() -> None:
        nonlocal chunk_rows, chunk_lines, chunk_bytes, total
        if not chunk_rows:
            return
        raw = b"".join(chunk_lines)
        compressed = _gzip(raw)
        digest = hashlib.sha256(compressed).hexdigest()
        key = f"chunks/{digest}.jsonl.gz"
        target = _member(destination, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(compressed)
        descriptors.append({
            "key": key, "sha256": digest, "bytes": len(compressed),
            "uncompressed_bytes": len(raw), "normalized_sha256": hashlib.sha256(raw).hexdigest(),
            "count": len(chunk_rows), "start_index": total,
            "first_time_utc_ms": int(chunk_rows[0]["time_utc_ms"]),
            "last_time_utc_ms": int(chunk_rows[-1]["time_utc_ms"]),
            "first_block": int(chunk_rows[0]["block"]), "last_block": int(chunk_rows[-1]["block"]),
            "first_order": list(_order(chunk_rows[0])), "last_order": list(_order(chunk_rows[-1])),
        })
        total += len(chunk_rows)
        chunk_rows, chunk_lines, chunk_bytes = [], [], 0

    def append_group() -> None:
        nonlocal chunk_bytes, group_rows, group_lines, group_bytes
        if chunk_rows and (len(chunk_rows) + len(group_rows) > max_events or chunk_bytes + group_bytes > max_bytes):
            emit_chunk()
        chunk_rows.extend(group_rows)
        chunk_lines.extend(group_lines)
        chunk_bytes += group_bytes
        group_rows, group_lines, group_bytes = [], [], 0

    for row in iter_jsonl(tape):
        # Validate every row, not merely a prefix. Preserve the original row values.
        TapeEvent.model_validate(row)
        order, event_time = _order(row), int(row["time_utc_ms"])
        if previous_order is not None and order <= previous_order:
            raise ChunkIntegrityError(f"tape order is not strictly increasing at {order}")
        if previous_time is not None and event_time < previous_time:
            raise ChunkIntegrityError(f"tape time moves backwards at {order}")
        if group_rows and event_time != previous_time:
            append_group()
        encoded = _line(row)
        if len(group_rows) + 1 > max_events or group_bytes + len(encoded) > max_bytes:
            raise PackError(
                f"equal-time event group at {event_time} exceeds max_events={max_events} or max_bytes={max_bytes}; "
                "increase the explicit chunk limits after measuring memory; no events were discarded"
            )
        canonical_digest.update(encoded)
        group_rows.append(row)
        group_lines.append(encoded)
        group_bytes += len(encoded)
        if row["kind"] == "cl_init":
            cl_init_ms.setdefault(row["pool"], event_time - pack.manifest.period.start_utc_ms)
        previous_order, previous_time = order, event_time
    append_group()
    emit_chunk()
    index = {
        "schema": CHUNK_SCHEMA, "pack_id": pack.pack_id,
        "start_utc_ms": pack.manifest.period.start_utc_ms,
        "source_hashes": source_hashes, "source_tape": tape.name,
        "normalized_tape_sha256": canonical_digest.hexdigest(),
        "max_events": max_events, "max_bytes": max_bytes,
        "total": total, "chunks": descriptors, "cl_init_ms": cl_init_ms, "metadata": metadata,
    }
    # Publish the index last: an interrupted export has no complete dataset index.
    index_bytes = json.dumps(index, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    temporary = destination / "index.json.tmp"
    temporary.write_bytes(index_bytes)
    temporary.replace(destination / "index.json")
    return index


class ChunkedTape(Sequence[RelEvent]):
    """Indexed event sequence retaining at most two decoded chunks by default.

    Iteration is streaming. A caller explicitly asking for a slice receives a
    list and must bound its slice; engine traversal should use integer indexing.
    ``time_at`` reads chunk endpoints from the index without opening their files.
    """

    def __init__(
        self, index: dict[str, Any], local_root: Path | str, start_utc_ms: int, *, max_cached_chunks: int = 2
    ) -> None:
        if type(max_cached_chunks) is not int or max_cached_chunks < 1:
            raise ValueError("max_cached_chunks must be a positive integer")
        if index.get("schema") != CHUNK_SCHEMA:
            raise ChunkIntegrityError("unsupported tape chunk index schema")
        self.index = index
        self.local_root = Path(local_root)
        self.start_utc_ms = start_utc_ms
        self.max_cached_chunks = max_cached_chunks
        self._cache: OrderedDict[int, list[RelEvent]] = OrderedDict()
        self._chunks = index["chunks"]
        self._starts: list[int] = []
        total, last_time, last_order = 0, None, None
        for chunk in self._chunks:
            count = chunk["count"]
            if type(count) is not int or count < 1 or chunk["start_index"] != total:
                raise ChunkIntegrityError("chunk event positions are not contiguous")
            if chunk["first_time_utc_ms"] > chunk["last_time_utc_ms"]:
                raise ChunkIntegrityError("chunk time range is reversed")
            first_order, end_order = tuple(chunk["first_order"]), tuple(chunk["last_order"])
            if first_order > end_order or (count > 1 and first_order == end_order):
                raise ChunkIntegrityError("chunk order range is invalid")
            if last_time is not None and chunk["first_time_utc_ms"] <= last_time:
                raise ChunkIntegrityError("chunks overlap or divide an equal-time event group")
            if last_order is not None and first_order <= last_order:
                raise ChunkIntegrityError("chunk order ranges overlap")
            if chunk["bytes"] < 1 or chunk["uncompressed_bytes"] < 1:
                raise ChunkIntegrityError("chunk size must be positive")
            if chunk["count"] > index["max_events"] or chunk["uncompressed_bytes"] > index["max_bytes"]:
                raise ChunkIntegrityError("chunk exceeds the declared bounds")
            _member(self.local_root, chunk["key"])
            self._starts.append(total)
            total += count
            last_time, last_order = chunk["last_time_utc_ms"], end_order
        if type(index["total"]) is not int or total != index["total"]:
            raise ChunkIntegrityError("chunk event count differs from index total")

    def __len__(self) -> int:
        return self.index["total"]

    @property
    def total(self) -> int:
        return len(self)

    @property
    def cached_chunk_count(self) -> int:
        return len(self._cache)

    def clear_cache(self) -> None:
        self._cache.clear()

    def _position(self, index: int) -> tuple[int, int]:
        if not isinstance(index, int):
            raise TypeError("tape indexes must be integers")
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError("tape index out of range")
        chunk_number = bisect.bisect_right(self._starts, index) - 1
        return chunk_number, index - self._starts[chunk_number]

    def chunk_for_index(self, index: int) -> dict[str, Any]:
        number, _offset = self._position(index)
        return dict(self._chunks[number])

    def chunk_end_index(self, index: int) -> int:
        """Exclusive end position for the chunk containing an event, without I/O."""
        chunk = self.chunk_for_index(index)
        return chunk["start_index"] + chunk["count"]

    def time_at(self, index: int) -> int:
        number, offset = self._position(index)
        chunk = self._chunks[number]
        if offset == 0:
            return chunk["first_time_utc_ms"] - self.start_utc_ms
        if offset == chunk["count"] - 1:
            return chunk["last_time_utc_ms"] - self.start_utc_ms
        return self._load(number)[offset].time_ms

    @overload
    def __getitem__(self, index: int) -> RelEvent: ...

    @overload
    def __getitem__(self, index: slice) -> list[RelEvent]: ...

    def __getitem__(self, index: int | slice) -> RelEvent | list[RelEvent]:
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        number, offset = self._position(index)
        return self._load(number)[offset]

    def __iter__(self) -> Iterator[RelEvent]:
        for number in range(len(self._chunks)):
            yield from self._load(number)

    def _load(self, number: int) -> list[RelEvent]:
        if number in self._cache:
            self._cache.move_to_end(number)
            return self._cache[number]
        chunk = self._chunks[number]
        path = _member(self.local_root, chunk["key"])
        if not path.is_file():
            raise MissingChunk(chunk, path)
        # Check size before allocation, then bound decompression even for corrupt gzip.
        if path.stat().st_size != chunk["bytes"]:
            raise ChunkIntegrityError("chunk compressed size mismatch")
        encoded = path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != chunk["sha256"]:
            raise ChunkIntegrityError("chunk compressed hash mismatch")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(encoded), mode="rb") as stream:
                raw = stream.read(chunk["uncompressed_bytes"] + 1)
        except (OSError, EOFError) as exc:
            raise ChunkIntegrityError("chunk gzip payload is invalid") from exc
        if len(raw) != chunk["uncompressed_bytes"] or hashlib.sha256(raw).hexdigest() != chunk["normalized_sha256"]:
            raise ChunkIntegrityError("chunk normalized size or hash mismatch")
        try:
            rows = [json.loads(line) for line in raw.splitlines()]
            if len(rows) != chunk["count"] or not rows:
                raise ChunkIntegrityError("chunk event count mismatch")
            if (_order(rows[0]) != tuple(chunk["first_order"]) or _order(rows[-1]) != tuple(chunk["last_order"])
                    or rows[0]["time_utc_ms"] != chunk["first_time_utc_ms"]
                    or rows[-1]["time_utc_ms"] != chunk["last_time_utc_ms"]):
                raise ChunkIntegrityError("chunk endpoints differ from index")
            previous_order, previous_time = None, None
            for row in rows:
                order, event_time = _order(row), int(row["time_utc_ms"])
                if previous_order is not None and (order <= previous_order or event_time < previous_time):
                    raise ChunkIntegrityError("chunk events are not ordered")
                previous_order, previous_time = order, event_time
            events = to_relative(rows, self.start_utc_ms)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ChunkIntegrityError):
                raise
            raise ChunkIntegrityError("chunk contains an invalid event") from exc
        # Evict before insertion so the retained cache never exceeds its bound.
        while len(self._cache) >= self.max_cached_chunks:
            self._cache.popitem(last=False)
        self._cache[number] = events
        return events
