"""Collect one full-detail UTC week on an offline worker, with a budget shared by every slice.

The hosted simulator never imports this module. The work directory contains private data and
must stay in private storage or an access-controlled Actions cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import shutil
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml

from market_replay.collectors.base import Budget, BudgetExhausted, HttpCollector, ReceiptStore
from market_replay.collectors.evm_rpc import RpcClient
from market_replay.collectors.historical import BLOCK_INTERVAL_MS, run_collection
from market_replay.datasets.builder import make_period
from market_replay.datasets.pack import Pack, compute_pack_id, sha256_file
from market_replay.datasets.validator import validate_pack
from market_replay.domain.models import DataObject

SCHEMA = "continuous_week_collection_v1"


class CollectionError(ValueError):
    """An operator-facing problem whose message contains no provider credential."""


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


class CumulativeTransport(httpx.BaseTransport):
    """Charge before a network attempt, including retries, and retain charges after failures."""

    def __init__(self, path: Path, *, max_requests: int, max_response_bytes: int, inner=None) -> None:
        self.path = path
        self.inner = inner if inner is not None else httpx.HTTPTransport()
        self.state = json.loads(path.read_text()) if path.exists() else {
            "requests": 0, "response_bytes": 0,
            "max_requests": max_requests, "max_response_bytes": max_response_bytes,
        }
        if self.state["max_requests"] != max_requests or self.state["max_response_bytes"] != max_response_bytes:
            raise CollectionError("the saved collection budget differs; review the existing ledger before changing it")
        write_json(self.path, self.state)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.state["requests"] >= self.state["max_requests"]:
            raise BudgetExhausted("cumulative RPC request cap reached; the workflow will not dispatch another slice")
        if self.state["response_bytes"] >= self.state["max_response_bytes"]:
            raise BudgetExhausted("cumulative RPC response byte cap reached")
        self.state["requests"] += 1
        write_json(self.path, self.state)
        response = self.inner.handle_request(request)
        try:
            content = response.read()
            self.state["response_bytes"] += len(content)
            write_json(self.path, self.state)
            if self.state["response_bytes"] > self.state["max_response_bytes"]:
                raise BudgetExhausted("the final RPC response crossed the cumulative byte cap; no further requests allowed")
            return response
        except BaseException:
            response.close()
            raise

    def close(self) -> None:
        self.inner.close()


def collection_config(start: str, end: str, root: Path, *, max_requests: int, max_response_bytes: int) -> dict[str, Any]:
    first = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC)
    last = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC)
    if last - first != timedelta(days=7):
        raise CollectionError("continuous week collection requires exactly seven UTC days, with an exclusive end")
    if max_requests <= 0 or max_response_bytes <= 0:
        raise CollectionError("collection caps must be positive")
    iso = lambda value: value.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return {
        "rpc_url_env": "BASE_RPC_URL", "chain": "base", "protocol": "all",
        "period_start_utc": iso(first), "period_end_utc": iso(last),
        "discovery_window_start_utc": iso(first - timedelta(days=7)), "prehistory_hours": 24,
        "selection_rule": "active_before_window_earliest_created_v1",
        "selection_rule_cl": "active_before_window_earliest_created_v1",
        "venues": ["uniswap_v2", "uniswap_v3", "uniswap_v4"],
        "venue_pairs": {"uniswap_v2": 4, "uniswap_v3": 4, "uniswap_v4": 8},
        "include_launches": True, "min_swaps": 1, "max_pairs": 16,
        "max_launches": 8, "launch_min_swaps": 20, "activity_lookback_blocks": 43200,
        "max_requests": max_requests, "max_response_bytes": max_response_bytes,
        "log_chunk_blocks": 10000, "initial_state_lookback_blocks": 20000,
        "availability_delay_ms": 4000,
        "out_dir": str((root / "collected" / f"base_week_{start}").resolve()),
        "authorization_note": "Operator-authorized read-only full-detail weekly collection; cumulative RPC caps; private historical data.",
    }


def peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def verify_boundary_headers(start_ms: int, end_ms: int, blocks: dict[str, int], headers: dict[str, int]) -> None:
    for label, target in (("start", start_ms), ("end", end_ms)):
        before, after = headers[f"{label}_before"], headers[f"{label}_at_or_after"]
        if not before < target <= after or after - before != BLOCK_INTERVAL_MS:
            raise CollectionError(f"{label} headers do not bracket the exact UTC boundary at the validated block interval")
    if headers["start_at_or_after"] != blocks["period_start_ts"]:
        raise CollectionError("the saved start block timestamp differs from the verified header")
    expected_end = headers["start_at_or_after"] + (blocks["period_end"] - blocks["period_start"]) * BLOCK_INTERVAL_MS
    if headers["end_at_or_after"] != expected_end:
        raise CollectionError("weekly block timing does not match the recorded interval")


def read_boundaries(root: Path, cfg: dict[str, Any], transport: CumulativeTransport) -> dict[str, Any]:
    target = root / "utc-boundaries.json"
    if target.exists():
        return json.loads(target.read_text())
    source = Path(cfg["out_dir"])
    work = source.with_name(source.name + "_work")
    blocks = json.loads((work / "uniswap_v2_work" / "checkpoints.json").read_text())["blocks"]
    budget = Budget(max_requests=cfg["max_requests"], max_response_bytes=cfg["max_response_bytes"], requests=transport.state["requests"])
    http = HttpCollector(provider="evm_rpc", budget=budget, receipts=ReceiptStore(root / "boundary-receipts", store_bodies=False), errors_path=root / "boundary-errors.jsonl", transport=transport)
    rpc = RpcClient(os.environ[cfg["rpc_url_env"]], http)
    headers = {}
    for label, number in (("start_before", blocks["period_start"] - 1), ("start_at_or_after", blocks["period_start"]), ("end_before", blocks["period_end"] - 1), ("end_at_or_after", blocks["period_end"])):
        timestamp = rpc.block_timestamp_ms(number)
        if timestamp is None:
            raise CollectionError(f"missing {label} block header; exact UTC boundaries cannot be established")
        headers[label] = timestamp
    start_ms = int(datetime.fromisoformat(cfg["period_start_utc"]).timestamp()) * 1000
    end_ms = int(datetime.fromisoformat(cfg["period_end_utc"]).timestamp()) * 1000
    verify_boundary_headers(start_ms, end_ms, blocks, headers)
    proof = {"schema": "utc_boundaries_v1", "start_utc_ms": start_ms, "end_utc_ms": end_ms, "end_exclusive": True, "blocks": blocks, "header_times_utc_ms": headers, "block_interval_ms": BLOCK_INTERVAL_MS, "event_timestamps_changed": False}
    write_json(target, proof)
    return proof


def prepare_exact_week(source: Path, destination: Path, proof: dict[str, Any]) -> Pack:
    """Create a new pack identity, preserving the collector output and every event byte."""
    original = Pack.load(source)
    start_ms, end_ms = proof["start_utc_ms"], proof["end_utc_ms"]
    verify_boundary_headers(start_ms, end_ms, proof["blocks"], proof["header_times_utc_ms"])
    if not (0 <= original.manifest.period.start_utc_ms - start_ms < BLOCK_INTERVAL_MS and 0 <= original.manifest.period.end_utc_ms - end_ms < BLOCK_INTERVAL_MS):
        raise CollectionError("collector period differs by more than the boundary block offset")
    before_end = proof["blocks"]["period_end"]
    for row in original.iter_tape():
        if int(row["time_utc_ms"]) >= end_ms or int(row["block"]) >= before_end:
            raise CollectionError("collector included an event outside the requested exclusive end; refusing to filter the tape")
        expected = proof["header_times_utc_ms"]["start_at_or_after"] + (int(row["block"]) - proof["blocks"]["period_start"]) * BLOCK_INTERVAL_MS
        if int(row["time_utc_ms"]) != expected:
            raise CollectionError("recorded event time disagrees with the verified block timeline")
    shutil.copytree(source, destination, dirs_exist_ok=True)
    boundary_file = destination / "utc-boundaries.json"
    write_json(boundary_file, {**proof, "source_pack_id": original.pack_id, "source_manifest_sha256": sha256_file(source / "manifest.yaml")})
    manifest = original.manifest.model_copy(deep=True)
    manifest.period = make_period(start_ms, end_ms, original.manifest.period.prehistory_start_utc_ms)
    manifest.data.objects = [obj for obj in manifest.data.objects if obj.filename != boundary_file.name]
    manifest.data.objects.append(DataObject(filename=boundary_file.name, schema_id="utc_boundaries_v1", size_bytes=boundary_file.stat().st_size, sha256=sha256_file(boundary_file)))
    manifest.pack_id = compute_pack_id({obj.filename: obj.sha256 for obj in manifest.data.objects}, manifest.execution.parameters_hash)
    manifest.provenance_notes.append("The new weekly pack uses exact requested UTC boundaries, proven by adjacent block headers. All original event bytes and daily packs are unchanged. See utc-boundaries.json.")
    (destination / "manifest.yaml").write_text(yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False))
    return Pack.load(destination)


def profile_pack(pack: Pack) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for row in pack.iter_tape():
        counts[row["kind"]] += 1
    files = [{"filename": path.name, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)} for path in sorted(pack.path.iterdir()) if path.is_file()]
    return {"pack_id": pack.pack_id, "duration_ms": pack.manifest.period.duration_ms, "pools": len(pack.pools), "assets": len(pack.assets), "events": sum(counts.values()), "events_by_kind": dict(counts), "stored_bytes": sum(item["size_bytes"] for item in files), "files": files}


def index_and_publish(pack: Pack, root: Path, profile: dict[str, Any], args: argparse.Namespace) -> None:
    from market_replay.datasets.chunks import export_pack_chunks

    started = time.monotonic()
    destination = root / "indexed" / pack.pack_id
    index = export_pack_chunks(pack.path, destination)
    index_bytes = (destination / "index.json").read_bytes()
    stored_bytes = len(index_bytes) + sum(item["bytes"] for item in index["metadata"].values()) + sum(item["bytes"] for item in index["chunks"])
    if index["total"] != profile["dataset"]["events"]:
        raise CollectionError("indexed export did not preserve the full event count")
    profile["export"] = {
        "elapsed_seconds": round(time.monotonic() - started, 3), "events": index["total"],
        "chunks": len(index["chunks"]), "stored_bytes": stored_bytes,
        "index_sha256": hashlib.sha256(index_bytes).hexdigest(),
        "normalized_tape_sha256": index["normalized_tape_sha256"],
        "largest_chunk_bytes": max((item["bytes"] for item in index["chunks"]), default=0),
        "largest_decoded_chunk_bytes": max((item["uncompressed_bytes"] for item in index["chunks"]), default=0),
        "storage_budget_bytes": args.storage_budget_bytes,
    }
    profile["status"] = "indexed"
    if not args.publish:
        return
    if stored_bytes > args.storage_budget_bytes:
        raise CollectionError("indexed week exceeds the explicit storage budget; retain all detail and review storage usage before publishing")
    from market_replay.service.datasets import publish_pack_chunks
    from market_replay.service.storage import SupabaseObjects

    objects = SupabaseObjects.from_env()
    try:
        descriptor = publish_pack_chunks(destination, objects, max_bytes=args.storage_budget_bytes)
    finally:
        objects.close()
    descriptor["name"] = f"base_week_{args.start}"
    # This is a candidate, outside weeks/catalog. Register only after actual weekly
    # runtime/checkpoint equivalence measurements have passed on the same pack id.
    write_json(root / "catalog-candidate.json", descriptor)
    profile["publication"] = {"status": "uploaded_private", "pack_id": pack.pack_id, "catalog_registered": False}
    profile["status"] = "uploaded_private"


def collect(args: argparse.Namespace) -> int:
    root = args.work_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    cfg = collection_config(args.start, args.end, root, max_requests=args.max_requests, max_response_bytes=args.max_response_bytes)
    config_path = root / "config.yaml"
    if config_path.exists() and not (root / "rpc-budget.json").exists():
        raise CollectionError("collection config exists without its cumulative budget; refusing to reset prior usage")
    if config_path.exists() and yaml.safe_load(config_path.read_text()) != cfg:
        raise CollectionError("saved collection config differs; use the original config to resume this private work directory")
    if not os.environ.get("BASE_RPC_URL"):
        raise CollectionError("BASE_RPC_URL is missing; the workflow reuses the existing RPC_URL secret")
    if args.publish and (not os.environ.get("SUPABASE_URL") or not (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SECRET_KEY"))):
        raise CollectionError("publishing requires the existing Supabase project's URL and server-only Storage credential in Actions")
    if args.storage_budget_bytes <= 0:
        raise CollectionError("storage budget must be positive; no plan upgrade is automatic")
    if not 0 < args.max_minutes <= 40:
        raise CollectionError("each collection slice must be greater than zero and at most 40 minutes")
    finished = datetime.fromisoformat(cfg["period_end_utc"])
    if finished > datetime.now(UTC) - timedelta(hours=12):
        raise CollectionError("the full week must have ended at least 12 hours ago")
    profile_path = root / "collection-profile.json"
    profile = json.loads(profile_path.read_text()) if profile_path.exists() else {"schema": SCHEMA, "start": args.start, "end": args.end, "slices": []}
    with CumulativeTransport(root / "rpc-budget.json", max_requests=args.max_requests, max_response_bytes=args.max_response_bytes) as transport:
        config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        started = time.monotonic()
        requests_before = transport.state["requests"]
        try:
            result_path = root / "last-result.json"
            previous = json.loads(result_path.read_text()) if result_path.exists() else {}
            result = previous if previous.get("status") == "pack_built" and (Path(cfg["out_dir"]) / "manifest.yaml").exists() else run_collection(config_path, root, transport=transport, store_bodies=False, budget_used=requests_before, deadline=started + args.max_minutes * 60)
            profile["slices"].append({"status": result["status"], "elapsed_seconds": round(time.monotonic() - started, 3), "requests_before": requests_before, "requests_after": transport.state["requests"], "peak_rss_bytes": peak_rss_bytes()})
            profile["status"] = result["status"]
            write_json(root / "last-result.json", result)
            if result["status"] in ("slice_expired", "in_progress_resumable"):
                return 3
            if result["status"] != "pack_built":
                print(f"collection stopped: {result['status']}; inspect the private last-result.json; no next slice dispatched", file=sys.stderr)
                return 1
            proof = read_boundaries(root, cfg, transport)
            pack = prepare_exact_week(Path(cfg["out_dir"]), root / "ready" / f"base_week_{args.start}", proof)
            validation_started = time.monotonic()
            validation = validate_pack(pack)
            write_json(pack.path / "validation.json", validation)
            profile["validation"] = {"elapsed_seconds": round(time.monotonic() - validation_started, 3), "peak_rss_bytes": peak_rss_bytes(), "qualification": validation["resulting_qualification"], "gates": [{"gate": gate["gate"], "status": gate["status"]} for gate in validation["gates"]]}
            profile["dataset"] = profile_pack(pack)
            if validation["resulting_qualification"] != "research" or validation["executable_failure"]:
                profile["status"] = "validation_failed"
                print("full week failed qualification; the complete dataset is retained privately and will not be published", file=sys.stderr)
                return 1
            profile["status"] = "validated"
            profile["ready_pack"] = str(pack.path.relative_to(root))
            index_and_publish(pack, root, profile, args)
            print(json.dumps({"status": profile["status"], "events": profile["dataset"]["events"], "stored_bytes": profile["export"]["stored_bytes"], "peak_rss_bytes": peak_rss_bytes(), "catalog_registered": False}))
            return 0
        finally:
            profile["budget"] = dict(transport.state)
            profile["peak_rss_bytes"] = peak_rss_bytes()
            write_json(profile_path, profile)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-09-07")
    parser.add_argument("--end", default="2026-09-14")
    parser.add_argument("--work-root", type=Path, default=Path("data/continuous-week-v1/2026-09-07_2026-09-14"))
    parser.add_argument("--max-minutes", type=float, default=40)
    parser.add_argument("--max-requests", type=int, default=40000)
    parser.add_argument("--max-response-bytes", type=int, default=16 * 1024**3)
    parser.add_argument("--publish", action="store_true", help="upload the validated indexed week to existing private Supabase Storage; does not register an episode")
    parser.add_argument("--storage-budget-bytes", type=int, default=750_000_000, help="explicit remaining Storage allowance confirmed before dispatch; never upgrades a plan")
    try:
        return collect(parser.parse_args())
    except Exception as error:
        # Provider exception strings may include an endpoint token. Keep details in private files.
        detail = str(error) if isinstance(error, (CollectionError, BudgetExhausted)) else "retain the private work directory and review config, budget, and coverage before retrying"
        print(f"collection stopped ({type(error).__name__}): {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
