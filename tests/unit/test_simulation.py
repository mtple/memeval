"""Acceptance 18-29: execution and accounting on the generated dev fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from market_replay.broker.ledger import AGENT_AVAILABLE, AGENT_PENDING, AGENT_RESERVED, SINK_GAS
from market_replay.datasets.generator import dev_short_config, generate_pack
from market_replay.datasets.pack import Pack
from market_replay.datasets.validator import reconcile_no_agent
from market_replay.domain.status import OrderState
from market_replay.engine.simulation import Simulation, SubmitRejected
from market_replay.venues.cpmm.pool import CpmmPoolState, FidelityLimit

BANK = 1_000_000


def sim_for(pack: Pack, **kw) -> Simulation:
    return Simulation(pack, bankroll_raw=kw.pop("bankroll", BANK), engine_seed="e", **kw)


def first_pool(sim: Simulation, t: int = 0) -> str:
    return sim.discovered_pools(t)[0]


def test_no_agent_replay_matches_generated_checkpoints(dev_pack: Pack):
    rec = reconcile_no_agent(dev_pack)
    assert rec["checkpoints"] > 0
    assert rec["mismatch_count"] == 0 and rec["fidelity_flag_count"] == 0
    sim = sim_for(dev_pack)
    sim.process_until(sim.end_ms)
    assert sim.reconciliation_mismatches == []
    # with no agent, private state equals the reference state at every pool
    for k, p in sim.pools.items():
        r = sim.ref_pools[k]
        assert (p.reserve0, p.reserve1) == (r.reserve0, r.reserve1)


def test_post_agent_flow_does_not_reset_private_reserves(dev_pack: Pack):
    sim = sim_for(dev_pack, bankroll=10**12)
    sim.process_until(300_000)
    pk = first_pool(sim, 300_000)
    pool = sim.pools[pk]
    amount = pool.reserve0 // 2000
    o, _ = sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=amount, min_amount_out=0, deadline_ms=400_000, idempotency_key="k")
    sim.process_until(320_000)
    assert o.state == OrderState.CONFIRMED
    # After many more external events (including sync checkpoints), private still differs from reference by the agent's impact
    sim.process_until(sim.end_ms)
    ref = sim.ref_pools[pk]
    assert pool.reserve0 != ref.reserve0 or pool.reserve1 != ref.reserve1
    assert pool.reserve0 * pool.reserve1 >= ref.reserve0 * ref.reserve1 - 1  # agent fees stay in the private pool
    assert sim.reconciliation_mismatches == []  # checkpoints reconcile the reference, not the private state


def test_split_orders_cannot_bypass_capacity(dev_pack: Pack):
    sim = sim_for(dev_pack, bankroll=10**12)
    sim.process_until(300_000)
    pk = first_pool(sim, 300_000)
    pool = sim.pools[pk]
    cap = dev_pack.params.capacity
    big = pool.reserve0 * cap.max_input_bps_of_reserve // 10_000 + 1
    with pytest.raises(SubmitRejected) as e:
        sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=big, min_amount_out=0, deadline_ms=10**7, idempotency_key="big")
    assert e.value.code == "MODEL_CAPACITY_LIMIT"
    # Split into many small pieces below the per-swap limit: cumulative displacement stops them
    piece = pool.reserve0 * cap.max_input_bps_of_reserve // 10_000 // 2
    accepted = 0
    rejected_cum = 0
    for i in range(40):
        try:
            sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=piece, min_amount_out=0, deadline_ms=10**7, idempotency_key=f"s{i}")
            accepted += 1
        except SubmitRejected as ex:
            assert ex.code == "MODEL_CAPACITY_LIMIT"
            rejected_cum += 1
        sim.process_until(sim.now_ms + 6000)
    assert rejected_cum > 0
    ref = sim.ref_pools[pk]
    disp = abs(pool.reserve0 - ref.reserve0) * 10_000
    assert disp <= ref.reserve0 * cap.max_cumulative_displacement_bps + piece * 10_000


def test_quote_expiry_changed_reserves_and_min_output(dev_pack: Pack):
    sim = sim_for(dev_pack, bankroll=10**12)
    sim.process_until(300_000)
    pk = first_pool(sim, 300_000)
    pool = sim.pools[pk]
    q = sim.quote(pk, pool.asset0, pool.reserve0 // 5000)
    sim.process_until(sim.now_ms + dev_pack.params.quote_ttl_ms + 1)
    with pytest.raises(SubmitRejected) as e:
        sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=q.amount_in, min_amount_out=0, deadline_ms=10**7, idempotency_key="q", quote_id=q.quote_id)
    assert e.value.code == "QUOTE_EXPIRED"
    # min-output failure at inclusion reverts and charges gas
    q2 = sim.quote(pk, pool.asset0, pool.reserve0 // 5000)
    o, _ = sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=q2.amount_in, min_amount_out=q2.amount_out + 10**30, deadline_ms=10**7, idempotency_key="minfail")
    cash_before = sim.ledger.balance(AGENT_AVAILABLE, sim.numeraire) + sim.ledger.balance(AGENT_RESERVED, sim.numeraire)
    sim.process_until(sim.now_ms + 10_000)
    assert o.state == OrderState.REVERTED and "SLIPPAGE_LIMIT" in (o.reason or "")
    assert o.gas_charged == dev_pack.params.gas_cost
    assert sim.ledger.balance(AGENT_AVAILABLE, sim.numeraire) == cash_before - dev_pack.params.gas_cost
    assert sim.ledger.balance(AGENT_RESERVED, sim.numeraire) == 0


def test_atomic_reservation_prevents_overspend(dev_pack: Pack):
    sim = sim_for(dev_pack, bankroll=100_000)
    sim.process_until(300_000)
    pk = first_pool(sim, 300_000)
    pool = sim.pools[pk]
    gas = dev_pack.params.gas_cost
    amt = 60_000
    sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=amt, min_amount_out=0, deadline_ms=10**7, idempotency_key="a")
    with pytest.raises(SubmitRejected) as e:
        sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=amt, min_amount_out=0, deadline_ms=10**7, idempotency_key="b")
    assert e.value.code == "INSUFFICIENT_FUNDS"
    assert sim.ledger.balance(AGENT_RESERVED, sim.numeraire) == amt + gas
    assert sim.ledger.balance(AGENT_AVAILABLE, sim.numeraire) == 100_000 - amt - gas


def test_duplicate_intent_and_conflicting_payload(dev_pack: Pack):
    sim = sim_for(dev_pack)
    sim.process_until(300_000)
    pk = first_pool(sim, 300_000)
    pool = sim.pools[pk]
    o1, c1 = sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=1000, min_amount_out=0, deadline_ms=10**7, idempotency_key="dup")
    o2, c2 = sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=1000, min_amount_out=0, deadline_ms=10**7, idempotency_key="dup")
    assert c1 and not c2 and o1 is o2
    with pytest.raises(SubmitRejected) as e:
        sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=1001, min_amount_out=0, deadline_ms=10**7, idempotency_key="dup")
    assert e.value.code == "IDEMPOTENCY_CONFLICT"
    sim.process_until(sim.now_ms + 10_000)
    assert len([o for o in sim.orders.values() if o.state == OrderState.CONFIRMED]) == 1
    # a retry after the fill still returns the same confirmed order, no duplicate fill
    o3, c3 = sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=1000, min_amount_out=0, deadline_ms=10**7, idempotency_key="dup")
    assert o3 is o1 and not c3


def test_lifecycle_gas_and_balance_effects(dev_pack: Pack):
    sim = sim_for(dev_pack)
    sim.process_until(300_000)
    pk = first_pool(sim, 300_000)
    pool = sim.pools[pk]
    gas = dev_pack.params.gas_cost
    start_cash = sim.ledger.balance(AGENT_AVAILABLE, sim.numeraire)
    o, _ = sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=5000, min_amount_out=0, deadline_ms=10**7, idempotency_key="lc")
    assert o.state == OrderState.PENDING_INCLUSION
    assert sim.ledger.balance(AGENT_RESERVED, sim.numeraire) == 5000 + gas
    assert o.inclusion_time_ms is not None
    sim.process_until(o.inclusion_time_ms)
    assert o.state == OrderState.FILLED_PENDING_CONFIRMATION
    assert sim.ledger.balance(AGENT_PENDING, pool.asset1) == o.amount_out
    assert sim.ledger.balance(AGENT_AVAILABLE, pool.asset1) == 0
    assert sim.ledger.balance(SINK_GAS, sim.numeraire) == gas
    sim.process_until(sim.schedule.time_of(o.confirm_block))
    assert o.state == OrderState.CONFIRMED
    assert sim.ledger.balance(AGENT_AVAILABLE, pool.asset1) == o.amount_out
    assert sim.ledger.balance(AGENT_PENDING, pool.asset1) == 0
    assert sim.ledger.balance(AGENT_AVAILABLE, sim.numeraire) == start_cash - 5000 - gas
    # expired: deadline before inclusion -> released, no gas
    o2, _ = sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=5000, min_amount_out=0, deadline_ms=sim.now_ms + 1, idempotency_key="exp")
    sim.process_until(sim.now_ms + 10_000)
    assert o2.state == OrderState.EXPIRED and o2.gas_charged == 0


def test_ledger_conservation(dev_pack: Pack):
    sim = sim_for(dev_pack)
    sim.process_until(300_000)
    pk = first_pool(sim, 300_000)
    pool = sim.pools[pk]
    sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=5000, min_amount_out=0, deadline_ms=10**7, idempotency_key="c")
    sim.process_until(sim.end_ms)
    for e in sim.ledger.entries:
        sums: dict[str, int] = {}
        for leg in e.legs:
            sums[leg.asset] = sums.get(leg.asset, 0) + leg.delta
        assert all(v == 0 for v in sums.values())


def test_no_route_positions_remain_inventory(tmp_path: Path):
    pack = generate_pack(dev_short_config(), tmp_path / "p")
    sim = Simulation(pack, bankroll_raw=BANK, engine_seed="e")
    # pool_02 gets a sell restriction at 70% of the episode; buy before that
    pk = "0:fixture_cpmm:pool_02"
    sim.process_until(max(sim.pool_discovery_ms[pk], 0) + 1000)
    pool = sim.pools[pk]
    o, _ = sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=5000, min_amount_out=0, deadline_ms=10**7, idempotency_key="buy")
    sim.process_until(sim.now_ms + 10_000)
    assert o.state == OrderState.CONFIRMED
    sim.process_until(int(sim.end_ms * 0.75))
    with pytest.raises(SubmitRejected) as e:
        sim.submit(pool_key=pk, asset_in=pool.asset1, asset_out=pool.asset0, amount_in=o.amount_out, min_amount_out=0, deadline_ms=10**7, idempotency_key="sell")
    assert e.value.code == "NO_ROUTE"
    v = sim.value_portfolio()
    h = [x for x in v.holdings if x["asset"] == pool.asset1]
    assert h and h[0]["class"] == "no_route" and h[0]["quantity"] == o.amount_out and h[0]["value"] == 0
    assert v.complete  # known modeled no-route is a known zero-recoverable value, inventory retained
    assert sim.ledger.balance(AGENT_AVAILABLE, pool.asset1) == o.amount_out


def test_missing_data_inventory_makes_headline_null(dev_pack: Pack):
    sim = sim_for(dev_pack)
    sim.process_until(300_000)
    pk = first_pool(sim, 300_000)
    pool = sim.pools[pk]
    o, _ = sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=5000, min_amount_out=0, deadline_ms=10**7, idempotency_key="b")
    sim.process_until(sim.now_ms + 10_000)
    assert o.state == OrderState.CONFIRMED
    # simulate the market leaving the validated domain
    sim.fidelity_failed[pk] = "ENVIRONMENT_FIDELITY_LIMIT"
    v = sim.value_portfolio()
    assert not v.complete and v.equity is None
    assert any(h["class"] == "unpriced_missing_data" for h in v.holdings)


def test_valuation_is_non_mutating_and_shares_liquidity(dev_pack: Pack):
    sim = sim_for(dev_pack)
    sim.process_until(300_000)
    pk = first_pool(sim, 300_000)
    pool = sim.pools[pk]
    o, _ = sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=20_000, min_amount_out=0, deadline_ms=10**7, idempotency_key="v")
    sim.process_until(sim.now_ms + 10_000)
    before = (pool.reserve0, pool.reserve1, sim.ledger.content_hash(), len(sim.orders))
    v1 = sim.value_portfolio()
    v2 = sim.value_portfolio()
    assert (pool.reserve0, pool.reserve1, sim.ledger.content_hash(), len(sim.orders)) == before
    assert v1.equity == v2.equity
    # Two holdings in the same pool would share liquidity: value(2x) < 2*value(x)
    branch = pool.copy()
    qty = pool.reserve1 // 100
    single = branch.copy().apply_swap(pool.asset1, qty)
    double = branch.copy().apply_swap(pool.asset1, 2 * qty)
    assert double < 2 * single


def test_end_of_week_no_free_exit_and_unresolved_kept(dev_pack: Pack):
    sim = sim_for(dev_pack)
    sim.process_until(sim.end_ms - 100)
    pk = first_pool(sim, sim.now_ms)
    pool = sim.pools[pk]
    o, _ = sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=5000, min_amount_out=0, deadline_ms=sim.end_ms + 60_000, idempotency_key="late")
    sim.finish()
    assert sim.finished and sim.now_ms >= sim.end_ms
    # order resolved inside the declared settlement tail (2 blocks) or remains pending; never dropped
    assert o.order_id in sim.orders
    assert o.state in (OrderState.CONFIRMED, OrderState.FILLED_PENDING_CONFIRMATION, OrderState.PENDING_INCLUSION)
    with pytest.raises(SubmitRejected) as e:
        sim.submit(pool_key=pk, asset_in=pool.asset0, asset_out=pool.asset1, amount_in=1, min_amount_out=0, deadline_ms=10**9, idempotency_key="after")
    assert e.value.code == "EPISODE_ENDED"
    v = sim.value_portfolio()
    # holdings stay inventory valued by the model; no cash appears from a fictional sale
    if o.state == OrderState.CONFIRMED:
        assert sim.ledger.balance(AGENT_AVAILABLE, pool.asset1) == o.amount_out
    assert v.cash_available + v.cash_reserved + v.cash_pending <= BANK


def test_counterfactual_burn_overdraw_is_fidelity_flag_not_negative_reserves():
    pool = CpmmPoolState(key="p", asset0="A", asset1="B", reserve0=100, reserve1=100)
    with pytest.raises(FidelityLimit):
        pool.apply_burn(150, 10)
    assert pool.reserve0 == 100 and pool.reserve1 == 100


def test_reference_burn_overdraw_flags_environment(tmp_path: Path):
    # Build a tiny pack where the agent drains enough that a later recorded burn overdraws the private pool.
    pack = generate_pack(dev_short_config(), tmp_path / "p")
    sim = Simulation(pack, bankroll_raw=10**14, engine_seed="e")
    # Inject a scripted burn event after the agent trades: simulate by direct call on private pool
    pk = sim.discovered_pools(0)[0]
    pool = sim.pools[pk]
    pool.reserve1 = 10  # extreme private divergence
    from market_replay.engine.tape import RelEvent

    ev = RelEvent(seq=10**6, block=sim.schedule.first_block + 1, log_index=0, time_ms=sim.now_ms, kind="burn", pool=pk, tx=None, wallet=None, asset_in=None, amount_in=None, amount_out_recorded=None, amount0=1, amount1=100, reserve0=None, reserve1=None, payload={}, available_ms=None, availability_basis="fixture")
    sim._apply_external(ev)
    assert pk in sim.fidelity_failed
    assert pool.reserve1 == 10  # unchanged, never negative
    assert any(f.code == "ENVIRONMENT_FIDELITY_LIMIT" for f in sim.fidelity_flags)
    with pytest.raises(SubmitRejected) as e:
        sim.quote(pk, pool.asset0, 1)
    assert e.value.code == "ENVIRONMENT_FIDELITY_LIMIT"
