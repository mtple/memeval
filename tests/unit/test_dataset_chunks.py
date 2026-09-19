from __future__ import annotations

import copy
import gzip
import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from market_replay.datasets import chunks
from market_replay.datasets.chunks import ChunkedTape, ChunkIntegrityError, MissingChunk, export_pack_chunks
from market_replay.datasets.pack import Pack, PackError, compute_pack_id, iter_jsonl, sha256_file, write_jsonl
from market_replay.engine.tape import to_relative


def source_pack(dev_pack, tmp_path: Path, *, count=12, per_time=2, compressed=False) -> Path:
    root = tmp_path / "source"
    shutil.copytree(dev_pack.path, root)
    original = next(dev_pack.iter_tape())
    rows = []
    for i in range(count):
        row = copy.deepcopy(original)
        row.update(seq=i, block=100 + i // per_time, log_index=i % per_time,
                   time_utc_ms=dev_pack.manifest.period.start_utc_ms + (i // per_time) * 1000)
        rows.append(row)
    if rows:
        rows[0]["kind"] = "cl_init"
    name = "tape.jsonl.gz" if compressed else "tape.jsonl"
    for old in (root / "tape.jsonl", root / "tape.jsonl.gz"):
        old.unlink(missing_ok=True)
    write_jsonl(root / name, rows)
    manifest = yaml.safe_load((root / "manifest.yaml").read_text())
    for obj in manifest["data"]["objects"]:
        if obj["filename"].startswith("tape.jsonl"):
            obj.update(filename=name, size_bytes=(root / name).stat().st_size, sha256=sha256_file(root / name))
    manifest["pack_id"] = compute_pack_id(
        {obj["filename"]: obj["sha256"] for obj in manifest["data"]["objects"]},
        manifest["execution"]["parameters_hash"],
    )
    (root / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    return root


@pytest.mark.parametrize("compressed", [False, True])
def test_chunk_export_preserves_every_row_and_metadata(dev_pack, tmp_path, compressed):
    source = source_pack(dev_pack, tmp_path, compressed=compressed)
    (source / "provenance.json").write_text('{"provider":"recorded-chain"}')
    (source / "private-rpc-workfile.json").write_text('{"must_not_copy":true}')
    output = tmp_path / "chunks"
    index = export_pack_chunks(source, output, max_events=5)
    pack = Pack.load(source)
    expected = list(pack.iter_tape())
    raw_chunks = [gzip.decompress((output / row["key"]).read_bytes()) for row in index["chunks"]]
    reconstructed = [json.loads(line) for raw in raw_chunks for line in raw.splitlines()]
    assert reconstructed == expected
    assert hashlib.sha256(b"".join(raw_chunks)).hexdigest() == index["normalized_tape_sha256"]
    assert index["source_hashes"][index["source_tape"]] == sha256_file(source / index["source_tape"])
    assert index["total"] == len(expected)
    assert index["cl_init_ms"] == {expected[0]["pool"]: 0}
    for name, descriptor in index["metadata"].items():
        assert (output / descriptor["key"]).read_bytes() == (source / name).read_bytes()
        assert sha256_file(output / descriptor["key"]) == descriptor["sha256"]
    assert "private-rpc-workfile.json" not in index["metadata"]
    assert not (output / "metadata/private-rpc-workfile.json").exists()
    actual = ChunkedTape(index, output, pack.manifest.period.start_utc_ms)
    assert list(actual) == to_relative(expected, pack.manifest.period.start_utc_ms)
    assert actual[-1] == actual[len(actual) - 1]
    assert actual[1:5:2] == [actual[1], actual[3]]
    assert actual.total == len(actual)
    with pytest.raises(IndexError):
        _ = actual[len(actual)]


def test_chunk_group_boundaries_and_deterministic_hashes(dev_pack, tmp_path):
    source = source_pack(dev_pack, tmp_path)
    first = export_pack_chunks(source, tmp_path / "one", max_events=5)
    second = export_pack_chunks(source, tmp_path / "two", max_events=5)
    assert first == second
    assert (tmp_path / "one/index.json").read_bytes() == (tmp_path / "two/index.json").read_bytes()
    assert [row["count"] for row in first["chunks"]] == [4, 4, 4]
    for left, right in zip(first["chunks"], first["chunks"][1:], strict=False):
        assert left["last_time_utc_ms"] < right["first_time_utc_ms"]
        assert left["start_index"] + left["count"] == right["start_index"]
    tape = ChunkedTape(first, tmp_path / "absent-files", first["start_utc_ms"])
    for chunk in first["chunks"]:
        assert tape.time_at(chunk["start_index"]) == chunk["first_time_utc_ms"] - first["start_utc_ms"]
        assert tape.chunk_end_index(chunk["start_index"]) == chunk["start_index"] + chunk["count"]
    assert tape.cached_chunk_count == 0


def test_equal_time_group_over_limit_fails_without_an_index(dev_pack, tmp_path):
    source = source_pack(dev_pack, tmp_path, count=8, per_time=8)
    output = tmp_path / "chunks"
    with pytest.raises(PackError, match="equal-time event group.*increase the explicit chunk limits"):
        export_pack_chunks(source, output, max_events=5)
    assert not (output / "index.json").exists()
    with pytest.raises(PackError, match="equal-time event group"):
        export_pack_chunks(source, output, max_bytes=1)


def test_loading_and_export_memory_are_bounded(dev_pack, tmp_path, monkeypatch):
    source = source_pack(dev_pack, tmp_path, count=500)
    output = tmp_path / "chunks"
    original_iter = chunks.iter_jsonl

    def witnessed_rows(path):
        for i, row in enumerate(original_iter(path)):
            if i == 100:
                assert list((output / "chunks").glob("*.gz")), "export materialized the tape before writing"
                assert not (output / "index.json").exists()
            yield row

    monkeypatch.setattr(chunks, "iter_jsonl", witnessed_rows)
    index = export_pack_chunks(source, output, max_events=16)
    calls = []
    original_relative = chunks.to_relative

    def measured_relative(rows, start):
        calls.append(len(rows))
        assert len(rows) <= 16
        return original_relative(rows, start)

    monkeypatch.setattr(chunks, "to_relative", measured_relative)
    tape = ChunkedTape(index, output, index["start_utc_ms"])
    assert calls == []
    for i in range(len(tape)):
        assert tape[i].seq == i
        assert tape.cached_chunk_count <= 2
    assert len(calls) == len(index["chunks"])
    tape[0]  # First chunk was evicted rather than keeping the full tape.
    assert len(calls) == len(index["chunks"]) + 1
    tape.clear_cache()
    assert tape.cached_chunk_count == 0


def test_missing_and_corrupt_objects_are_rejected(dev_pack, tmp_path):
    source = source_pack(dev_pack, tmp_path)
    output = tmp_path / "chunks"
    index = export_pack_chunks(source, output, max_events=4)
    tape = ChunkedTape(index, output, index["start_utc_ms"])
    descriptor = index["chunks"][0]
    path = output / descriptor["key"]
    original = path.read_bytes()
    path.unlink()
    with pytest.raises(MissingChunk) as failure:
        _ = tape[0]
    assert failure.value.descriptor == descriptor
    assert failure.value.path == path
    path.write_bytes(original[:-1])
    with pytest.raises(ChunkIntegrityError, match="compressed size"):
        _ = tape[0]
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(ChunkIntegrityError, match="compressed hash"):
        _ = tape[0]
    path.write_bytes(original)
    tampered = copy.deepcopy(index)
    tampered["chunks"][0]["normalized_sha256"] = "0" * 64
    with pytest.raises(ChunkIntegrityError, match="normalized size or hash"):
        _ = ChunkedTape(tampered, output, index["start_utc_ms"])[0]
    assert tape[0].seq == 0


def test_bad_index_positions_and_private_path_escape_are_rejected(dev_pack, tmp_path):
    source = source_pack(dev_pack, tmp_path)
    output = tmp_path / "chunks"
    index = export_pack_chunks(source, output, max_events=4)
    bad = copy.deepcopy(index)
    bad["chunks"][1]["start_index"] += 1
    with pytest.raises(ChunkIntegrityError, match="contiguous"):
        ChunkedTape(bad, output, index["start_utc_ms"])
    bad = copy.deepcopy(index)
    bad["chunks"][0]["key"] = "../outside.gz"
    with pytest.raises(ChunkIntegrityError, match="invalid private dataset"):
        ChunkedTape(bad, output, index["start_utc_ms"])
    bad = copy.deepcopy(index)
    bad["chunks"][1]["first_time_utc_ms"] = bad["chunks"][0]["last_time_utc_ms"]
    with pytest.raises(ChunkIntegrityError, match="equal-time event group"):
        ChunkedTape(bad, output, index["start_utc_ms"])


def test_source_order_is_checked_without_silently_sorting(dev_pack, tmp_path, monkeypatch):
    source = source_pack(dev_pack, tmp_path)
    tape = source / "tape.jsonl"
    rows = list(iter_jsonl(tape))
    rows[2], rows[3] = rows[3], rows[2]
    monkeypatch.setattr(chunks, "iter_jsonl", lambda _path: iter(rows))
    with pytest.raises(ChunkIntegrityError, match="not strictly increasing"):
        export_pack_chunks(source, tmp_path / "chunks")


def test_empty_tape_has_a_valid_zero_event_index(dev_pack, tmp_path):
    source = source_pack(dev_pack, tmp_path, count=0)
    output = tmp_path / "chunks"
    index = export_pack_chunks(source, output)
    assert index["total"] == 0
    assert index["chunks"] == []
    assert index["normalized_tape_sha256"] == hashlib.sha256(b"").hexdigest()
    tape = ChunkedTape(index, output, index["start_utc_ms"])
    assert list(tape) == []
    with pytest.raises(IndexError):
        tape.time_at(0)
