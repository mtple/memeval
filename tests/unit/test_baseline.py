"""The per-day market baseline: a naive fixed stake in every pool, sold back into the pool at the close."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from tests.unit.test_engine_clmm import make_cl_pack

from market_replay.datasets.baseline import (
    BASELINE_FILE,
    _cpmm_out,
    baseline_sentence,
    compute_market_baseline,
    read_market_baseline,
    write_market_baseline,
)
from market_replay.datasets.pack import Pack


def test_cpmm_out_is_the_pool_formula():
    # 1 in on 1000/1000 reserves at 0.3% fee
    assert _cpmm_out(1000, 1_000_000, 1_000_000, 997, 1000) == Fraction(
        997 * 1_000_000 * 1000, 1_000_000 * 1000 + 997 * 1000
    )
    assert _cpmm_out(0, 1, 1, 997, 1000) == 0 and _cpmm_out(5, 0, 1, 997, 1000) == 0


def test_generated_pack_baseline_is_bounded_and_consistent(dev_pack: Pack):
    b = compute_market_baseline(dev_pack)
    assert b["numeraire_hold_return"] == "0.000000" and b["stake_raw"] == str(
        10 ** (dev_pack.manifest.numeraire_decimals - 2)
    )
    for basket in (b["launches"], b["established"], b["all_pools"]):
        if not basket["pools_priced"]:
            continue
        for k in (
            "equal_weight_return",
            "median_return",
            "best_return",
            "worst_return",
            "p10_return",
            "p90_return",
        ):
            assert float(basket[k]) >= -1.0
        assert float(basket["worst_return"]) <= float(basket["median_return"]) <= float(basket["best_return"])
        for k in ("share_up", "share_down", "share_drained"):
            assert 0.0 <= float(basket[k]) <= 1.0
        assert float(basket["share_up"]) + float(basket["share_down"]) <= 1.0 + 1e-9
    assert (
        b["all_pools"]["pools"] == b["launches"]["pools"] + b["established"]["pools"] == len(dev_pack.pools)
    )
    assert b["caveats"]


def test_cl_pack_splits_launches_from_established(tmp_path: Path):
    pack = make_cl_pack(tmp_path / "cl")
    b = compute_market_baseline(pack)
    # POOL1 traded before the window, POOL2 was initialized inside it
    assert b["established"]["pools"] == 1 and b["launches"]["pools"] == 1
    assert b["launches"]["pools_priced"] == 1 and b["established"]["pools_priced"] == 1
    assert float(b["launches"]["share_drained"]) == 0.0  # its one position was never burned
    assert float(b["launches"]["equal_weight_return"]) >= -1.0


def test_sidecar_round_trip_does_not_touch_the_pack(tmp_path: Path):
    d = tmp_path / "cl"
    make_cl_pack(d)
    before = Pack.load(d).pack_id
    written = write_market_baseline(d)
    assert (d / BASELINE_FILE).exists() and written["pack_id"] == before
    assert Pack.load(d).pack_id == before  # not a hashed object
    read = read_market_baseline(d)
    assert read == json.loads((d / BASELINE_FILE).read_text()) == written
    assert read_market_baseline(tmp_path / "nowhere") is None
    sentence = baseline_sentence(read)
    assert sentence and "Market that day" in sentence and "0%" in sentence
    assert baseline_sentence(None) is None and baseline_sentence({"launches": {"pools_priced": 0}}) is None
