"""On-disk pack layout and loader.

A pack directory contains::

    manifest.yaml            private manifest (EpisodeManifest)
    execution_params.yaml    ExecutionParams
    assets.jsonl             Asset records
    pools.jsonl              Pool records
    tape.jsonl               TapeEvent records ordered by (block, log_index, seq)
    blocks.jsonl             optional: {"block": n, "time_utc_ms": t} rows (historical packs)
    restrictions.jsonl       optional: RestrictionObservation rows
    coverage.json            coverage ledger / report
    validation.json          qualification gate report
    inventory.json           optional: unsupported/missing pool inventory with reasons

Packs are immutable: the manifest lists every data object with its sha256 and the
``pack_id`` is content-addressed over those hashes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..domain.models import Asset, EpisodeManifest, Pool, RestrictionObservation
from .execution_params import ExecutionParams

DATA_FILES = ("assets.jsonl", "pools.jsonl", "tape.jsonl", "tape.jsonl.gz", "blocks.jsonl", "restrictions.jsonl", "inventory.json")


class PackError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _open_text(path: Path, mode: str):
    """JSON lines files may be gzip-compressed (``*.jsonl.gz``): a recorded week's tape is large and
    never changes, and the deployment bundle that carries every week has a size limit."""
    if path.suffix == ".gz":
        return gzip.open(path, mode + "t", encoding="utf-8")
    return path.open(mode, encoding="utf-8")


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with _open_text(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_jsonl(path: Path, rows: Iterator[dict[str, Any]] | list[dict[str, Any]]) -> int:
    n = 0
    with _open_text(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":"), sort_keys=True))
            f.write("\n")
            n += 1
    return n


LAZY_TAPE_ROWS = 150_000


def count_jsonl(path: Path) -> int:
    with _open_text(path, "r") as f:
        return sum(1 for line in f if line.strip())


def tape_path(pack_dir: Path) -> Path | None:
    """The tape file a pack carries: compressed when it was written that way."""
    for name in ("tape.jsonl.gz", "tape.jsonl"):
        if (pack_dir / name).exists():
            return pack_dir / name
    return None


def compute_pack_id(object_hashes: dict[str, str], params_hash: str) -> str:
    h = hashlib.sha256()
    for name in sorted(object_hashes):
        h.update(f"{name}:{object_hashes[name]}\n".encode())
    h.update(f"execution_params:{params_hash}\n".encode())
    return "pack_" + h.hexdigest()[:32]


@dataclass(slots=True)
class Pack:
    path: Path
    manifest: EpisodeManifest
    params: ExecutionParams
    assets: dict[str, Asset]
    pools: dict[str, Pool]
    tape: list[dict[str, Any]]  # raw rows (validated by the pack validator; fast path for the engine); empty when lazy
    blocks: list[tuple[int, int]] = field(default_factory=list)  # (block, time_utc_ms) sorted
    tape_file: Path | None = None
    tape_lazy: bool = False  # a long tape stays on disk: ``iter_tape`` streams it, ``tape`` is empty
    tape_rows: int = 0
    restrictions: list[RestrictionObservation] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    inventory: dict[str, Any] = field(default_factory=dict)

    @property
    def pack_id(self) -> str:
        return self.manifest.pack_id

    def iter_tape(self) -> Iterator[dict[str, Any]]:
        """Every tape row in order: from memory when loaded, streamed from disk when the tape is lazy."""
        if self.tape or not self.tape_lazy:
            yield from self.tape
            return
        if self.tape_file is not None:
            yield from iter_jsonl(self.tape_file)

    @property
    def tape_count(self) -> int:
        return len(self.tape) if self.tape or not self.tape_lazy else self.tape_rows

    @property
    def numeraire(self) -> str:
        return self.manifest.numeraire

    @classmethod
    def load(cls, path: Path | str, *, verify_hashes: bool = True, load_tape: bool = True) -> Pack:
        p = Path(path)
        if not (p / "manifest.yaml").exists():
            raise PackError(f"no manifest.yaml in {p}")
        manifest = EpisodeManifest.model_validate(yaml.safe_load((p / "manifest.yaml").read_text()))
        params = ExecutionParams.load(p / manifest.execution.parameters_file)
        if params.content_hash() != manifest.execution.parameters_hash:
            raise PackError("execution parameters hash mismatch; pack is not immutable")
        if verify_hashes:
            for obj in manifest.data.objects:
                fp = p / obj.filename
                if not fp.exists():
                    raise PackError(f"missing data object {obj.filename}")
                actual = sha256_file(fp)
                if actual != obj.sha256:
                    raise PackError(f"hash mismatch for {obj.filename}")
            expected = compute_pack_id({o.filename: o.sha256 for o in manifest.data.objects}, params.content_hash())
            if expected != manifest.pack_id:
                raise PackError("pack_id does not match content hashes")
        assets = {a["key"]: Asset.model_validate(a) for a in iter_jsonl(p / "assets.jsonl")}
        pools = {r["key"]: Pool.model_validate(r) for r in iter_jsonl(p / "pools.jsonl")}
        tape: list[dict[str, Any]] = []
        tp = tape_path(p)
        tape_rows = 0
        tape_lazy = False
        if load_tape and tp is not None:
            tape_rows = count_jsonl(tp)
            if tape_rows >= LAZY_TAPE_ROWS:
                tape_lazy = True  # every consumer streams it; row dicts of a week of every launch would not fit in memory
            else:
                tape = list(iter_jsonl(tp))
        blocks: list[tuple[int, int]] = []
        if (p / "blocks.jsonl").exists():
            blocks = sorted((int(r["block"]), int(r["time_utc_ms"])) for r in iter_jsonl(p / "blocks.jsonl"))
        restrictions: list[RestrictionObservation] = []
        if (p / "restrictions.jsonl").exists():
            restrictions = [RestrictionObservation.model_validate(r) for r in iter_jsonl(p / "restrictions.jsonl")]
        coverage = json.loads((p / "coverage.json").read_text()) if (p / "coverage.json").exists() else {}
        validation = json.loads((p / "validation.json").read_text()) if (p / "validation.json").exists() else {}
        inventory = json.loads((p / "inventory.json").read_text()) if (p / "inventory.json").exists() else {}
        return cls(
            path=p,
            manifest=manifest,
            params=params,
            assets=assets,
            pools=pools,
            tape=tape,
            blocks=blocks,
            tape_file=tp,
            tape_lazy=tape_lazy,
            tape_rows=tape_rows if tp is not None else 0,
            restrictions=restrictions,
            coverage=coverage,
            validation=validation,
            inventory=inventory,
        )
