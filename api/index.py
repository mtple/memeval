"""Vercel Python Function entrypoint: the whole Market Replay control plane and agent plane as one FastAPI app."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "sdk" / "python"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from market_replay.service.hosted import build_hosted_app  # noqa: E402

app, _manager = build_hosted_app()
