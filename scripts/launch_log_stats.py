"""How big would the tape be at each minimum swap count? Reads the kept logs of a launch collection
(weeks/<name>_work/**/logs/events_*.jsonl) and prints, per threshold, the pools kept and the share of
logs they carry. Used from the collect-week workflow's "stats" mode; needs no RPC."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from market_replay.collectors.evm_rpc import TOPIC_SWAP, TOPIC_V3_SWAP, TOPIC_V4_SWAP

SWAPS = {TOPIC_SWAP.lower(), TOPIC_V3_SWAP.lower(), TOPIC_V4_SWAP.lower()}


def main(root: Path) -> None:
    files = sorted(root.glob("**/logs/events_*.jsonl"))
    swaps: dict[str, int] = defaultdict(int)
    logs: dict[str, int] = defaultdict(int)
    total = 0
    nbytes = 0
    for f in files:
        v4 = "_v4" in f.name
        with f.open() as fh:
            for line in fh:
                nbytes += len(line)
                lg = json.loads(line)
                key = (f.name.split("_")[1] + ":" + (lg["topics"][1] if v4 else lg["address"])).lower()
                logs[key] += 1
                total += 1
                if lg["topics"][0].lower() in SWAPS:
                    swaps[key] += 1
    out = {"files": [str(f.relative_to(root)) for f in files], "pools_with_logs": len(logs), "logs": total, "jsonl_bytes": nbytes, "by_min_swaps": {}}
    for n in (1, 2, 3, 5, 10, 20, 50, 100, 200, 500, 1000, 5000):
        keep = [k for k in logs if swaps[k] >= n]
        kept_logs = sum(logs[k] for k in keep)
        out["by_min_swaps"][str(n)] = {"pools": len(keep), "logs": kept_logs, "share_of_logs": round(kept_logs / total, 3) if total else None}
    top = sorted(logs.items(), key=lambda kv: -kv[1])[:20]
    out["busiest_pools"] = [{"pool": k, "logs": v, "swaps": swaps[k]} for k, v in top]
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("weeks"))
