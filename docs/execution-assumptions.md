# Execution assumptions

## Model `cpmm_fixed_flow_v1`

Plain constant-product pools only (`fixture_cpmm`, `uniswap_v2_plain`). Exact-input swap:

```
out = floor((a * f_n * y) / (x * f_d + a * f_n));  x += a;  y -= out
```

with `f_n/f_d = 997/1000` for canonical v2 pools. Fees are embedded in the output and are
never charged again in PnL; reports show the implicit fee as information only.

Fixed external flow, mutable private market:

1. The tape is the ordered external primitive actions (swap, mint, burn, sync checkpoint,
   halt, fixture restriction).
2. Each participant has its own private copy of every pool.
3. The participant's fills change the private reserves permanently; loading a later `sync`
   never resets them (checkpoints reconcile only the reference state).
4. Later external swaps keep their recorded input direction and amount; their output is
   recomputed on the private state. If a recorded output was below the model maximum on the
   reference state, the recorded/max ratio is preserved.
5. Mints and burns are fixed token transfers. A burn that would overdraw private reserves
   raises `ENVIRONMENT_FIDELITY_LIMIT`: the pool is frozen for execution, holdings become
   `unpriced_missing_data`, reserves are never negative and never reset.
6. Multi-input / multi-output (flash-swap-like) shapes are rejected at normalization.
7. The no-agent path must reproduce every `sync` checkpoint exactly, or the pack is not
   executable with this adapter.

This is **historical-flow-based simulation**, not "the prices that would have happened".
Other participants' reactions to the evaluated agent are not modeled.

## Capacity guardrails (`capacity_v1`, unvalidated)

Per swap: input ≤ `max_input_bps_of_reserve` of the current input reserve (fixtures 100 bps,
research profile 10 bps). Cumulative: private reserves must stay within
`max_cumulative_displacement_bps` of the no-agent reference (fixtures 500, research 50).
Checked at submit and again at inclusion; the cumulative check makes order splitting
ineffective. A rejection is a model limit (`MODEL_CAPACITY_LIMIT`), not a market refusal, and
charges no gas. Quotes report `capacity_ok`.

## Timing

Fixture profile (`fixture_default_v1`, test settings): 2000 ms blocks, 100 ms data latency,
100 ms quote latency, 500 ms submit processing, inclusion in the first block at or after
readiness (after that block's external events), confirmation one block later, 5000 ms quote
TTL, 1500 ms observation availability delay, settlement tail of 2 blocks. Research profile
(`base_research_v1`, assumptions): 2000 ms Base blocks verified by sampled headers, 250 ms
data/quote latency, 1000 ms submit, 4000 ms availability delay, gas the sampled median of the period's recorded swaps (0 until a recorded
fee series is imported. None of these are measured Base latencies.

The competence-oriented suite charges these fixed latencies, not an LLM's wall-clock; wall
clock and inference usage are recorded separately.

## Gas

`gas_cost_raw` per included transaction (fill or revert) in the numeraire, reserved at
submit together with the principal. Fixtures: 50 raw CASH (artificial sensitivity value).
Real days: the median of `gasUsed x effectiveGasPrice + l1Fee` over a sample of about 200 of the
swaps recorded in the period (`gas_basis` names the sample size; `coverage.gas_sample` holds the
quartiles and the L1 share). One number for the whole day, not the fee at each block.
Pre-submission rejections pay nothing.

## Valuation policy `liquidate_all_holdings_via_direct_pool_v1`

On a temporary branch copy of the private pools: sell each holding (available + reserved +
pending) into the deepest discovered pool to the numeraire in canonical order, subtracting
gas per sale; shared liquidity is consumed sequentially, never counted twice. Classes:
`cash`, `priced_liquidatable`, `no_route` (halted / sell-blocked / no pool: zero recoverable
value under the model, inventory retained), `unpriced_missing_data` (unsupported mechanics,
missing state, fidelity failure → `headline_return: null`). Valuation never mutates the run.
Equity grid: every `reporting_grid_ms` plus ledger events; missing points are gaps, not flat
lines; drawdown uses complete points only.

## Episode end

`session.finish` stops new actions, processes to the end, then applies the declared settlement
tail only to already submitted orders. No forced last-price sale. Unresolved orders and
inventory are reported as such.

## Restrictions

Fixture `restriction` events (e.g. `sell_blocked`) apply at their event time and become
visible at their availability time. Historical packs assume standard transfers
(`assumed_standard_transfer`) and cannot claim to measure honeypot/tax/sell-restriction
detection; recorded restriction observations have `available_utc_ms: null` because no
historical as-of service exists.
