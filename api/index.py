"""Vercel Python Function entrypoint: the whole Market Replay control plane and agent plane as one FastAPI app.

Vercel's build step parses this file and only treats it as a function when it finds a plain
top-level ``app = ...`` assignment, so the app must not be bound by tuple unpacking.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "sdk" / "python"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from market_replay.service.hosted import build_hosted_app  # noqa: E402

_built = build_hosted_app()
app = _built[0]
_manager = _built[1]
