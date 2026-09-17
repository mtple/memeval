# Accuracy roadmap for real-market simulation

Research digest (September 2026) and the build order it implies. Every item names its data
source and what the report will say about it. Sources are listed at the end of each section;
claims marked (unverified) came from search excerpts because the primary page was unreachable.

## Where memecoin trading on Base actually happens

- Bankr and Clanker (v4, mid-2025 onward) tokens trade on **Uniswap v4** pools with Clanker
  hooks: a dynamic-fee PoolKey, single-sided "staircase" liquidity locked by the Clanker locker,
  a 20% protocol cut of the LP fee taken through hook deltas, and MEV modules in the first two
  minutes (2-block delay or sniper auctions with a parabolic fee decay from about 80%).
  Bankr launches use a 0.7% pool fee (unverified). Older Clanker v3/v3.1 tokens sit on
  Uniswap v3 1% pools.
- Aerodrome is the largest Base DEX by volume (50 to 63% of trades, mostly blue-chip, stable
  and FX pairs), not where memecoins launch.
- Consequence: the Uniswap v2 collector covers a narrow slice. Coverage order: Uniswap v4 with
  Clanker hook semantics, then Uniswap v3 (shared math), then Aerodrome volatile v2 (near-free
  on the CPMM engine), Slipstream last.
- Uniswap v3 state is fully reconstructible from logs (Initialize, Mint, Burn carry ranges and
  amounts; the tick bitmap is derivable). Each replayed swap is verifiable against the Swap
  event's `sqrtPriceX96`, `liquidity` and `tick`, so CL reconciliation can be exact, like the v2
  Sync checks today. A mid-history start needs `eth_call` snapshots (`slot0`, `liquidity`,
  `ticks`) or a full index from pool creation.
- Uniswap v4: the Swap event carries the LP `fee` used, so historical dynamic fees replay ex
  post; counterfactual agent trades need the hook's fee rule (static per-direction, volatility
  accumulator, auction decay) and the 20% protocol cut.

Addresses (Base): Uniswap v3 factory `0x33128a8fC17869897dcE68Ed026d694621f6FDfD`; Uniswap v4
PoolManager `0x498581fF718922c3f8e6A244956aF099B2652b2b`, StateView
`0xa3c0c9b65bad0b08107aa264b0f3db444b867a71`; Clanker factory v4.0
`0xE85A59c628F7d27878ACeB4bf3b35733630083a9`; Aerodrome PoolFactory
`0x420DD381b31aEf6683db6B902084cB0FFECe40Da`. Clanker hooks have several deployments; read the
hook from `TokenCreated`/`Initialize`, never hardcode.

Sources: clanker-devco/v4-contracts and v3.1-contracts on GitHub, paragraph.com/@dish
(Clanker v4), docs.bankr.bot token launching (unverified), Uniswap v3-core and v4-core
interfaces, aerodrome-finance/contracts and slipstream, DefiLlama Base DEX volumes (via
secondary pages).

## Gas on Base (OP Stack, Fjord/Isthmus era)

- `fee = gasUsed * (baseFeePerGas + priorityFee) + l1Fee (+ operator fee, 0 on Base as far as
  verified)`, with the Fjord L1 data fee
  `l1Fee = max(100e6, -42585600 + 836500 * fastlzSize) * (l1BaseFeeScalar * l1BaseFee * 16 +
  l1BlobFeeScalar * l1BlobBaseFee) / 1e12`.
- Every L2 block's first transaction is the L1-attributes deposit whose calldata packs the
  scalars, L1 base fee and blob base fee, so `eth_getBlockByNumber(n, true)` gives a complete
  per-block gas input series with no receipts. Receipts expose `l1Fee`, `l1GasPrice`,
  `l1BlobBaseFee`, `l1BaseFeeScalar`, `l1BlobBaseFeeScalar` for calibration.
- Per-venue constants: v2 router swap 100k to 130k gas, about 120 to 160 estimated bytes; v3
  110k to 150k; universal router 150k to 250k. Calibrate `G` and `S` per venue from a few
  hundred receipts of the same week. Expected per-swap error 10 to 20% calibrated, 30 to 50%
  with generic constants; absolute cost $0.005 to $0.10 typical, so small relative to trades
  above a few hundred dollars but decisive for small bankrolls and frequent traders.

Sources: ethereum-optimism/specs fjord and isthmus exec-engine, optimism docs fees,
docs.base.org network fees (unverified), receipt field docs.

## MEV and market reaction

- Base has a single sequencer, no public mempool, Flashblocks (200 ms sub-blocks ordered by
  priority fee within a flashblock). Sandwiching on private-mempool rollups is rare and mostly
  unprofitable (arXiv 2601.19570, Jan 2026); the live MEV forms are backrun arbitrage and
  priority-fee races at launches (Nov 2025 creator-coin sniping).
- Detection from a week of logs: group Swap events by (block, pool) ordered by tx index;
  sandwich = A, V, B with A and V same direction, B opposite, same sender or contract for A
  and B, B input about A output, B profitable; backrun = opposite-direction swap within k
  transactions from a contract that also touches another pool of the pair in the same tx.
  Report per pool: sandwiches per 1000 swaps, victim size distribution, realised loss.
- Agent cost model: `E[cost] = fee + priceImpact(x) + p(x) * slippage + gas`, with `p(x)`
  measured per size bucket (expected near zero) and a reversion step after each simulated
  trade: `d_{t+1} = d_t * 2^(-1/H)` outside a no-arb band `d_min = pool fee + gas / notional`.
  Estimate `H` from the decay of deviations after large recorded swaps; expect same-block
  restoration for deviations above the band. No published Base half-life exists; measure it.

Sources: arXiv 2601.19570, 2506.14768, 2506.01462, 2406.02172, flashbots rollup-boost
flashblocks spec, Base block-building docs (unverified).

## Token behaviour (taxes, limits, honeypots)

- Clanker and Bankr tokens are plain ERC-20s; their fee lives in the pool (v4 hook), so the
  token-tax problem is smaller than the coverage problem for them. Other Base memecoins carry
  1 to 5% sell taxes, max-tx and max-wallet caps, trading switches, blacklists and honeypots.
- Detection from logs the collector already fetches plus ERC-20 Transfer events of the same
  transactions: buy tax = 1 - Transfer(pair to recipient) / Swap.amountOut (median over 5+
  buys, flag >= 0.5%); sell tax from the tax-wallet transfer alongside the pair transfer;
  honeypot suspicion when 20+ buyers over 3+ days and zero sells (report as inferred absence,
  never asserted); max-tx from clustering of the largest buys at round fractions of supply;
  reflection/rebase from `balanceOf(pair)` diverging from Sync reserves. Uniswap v3 pools
  cannot take sell-taxed tokens at all (the callback reverts), which is itself a signal.
- Model: a per-pool restriction record versioned by block range with a basis in
  {observed-logs, eth_call-getter, eth_call-simulation, inferred-absence, unknown}; apply
  measured taxes to fills, refuse sells for honeypots with a typed error, apply caps as hard
  limits only when the sample is large. Cross-check a sample against GoPlus and honeypot.is.

Sources: Uniswap v2 common errors, SlowMist v3 audit notes, GoPlus token security API,
honeypot.is docs (unverified), arXiv 2109.00229 and 2201.07220.

## Provider limits and validation

- `eth_getLogs` block-range caps: Coinbase Developer Platform 1,000 blocks (30 BU per call,
  10M BU per month free, about 50 calls per second); Alchemy 2,000 blocks without a result cap
  or any range under 10k results; QuickNode paid 10,000; dRPC 10,000 nominal, 2,000
  recommended; public mainnet.base.org throttled and unreliable. Batch all pools into one call
  per chunk (addresses array) and split by address only on result-cap errors: a week of 16 to
  200 pools is a few hundred calls, not thousands.
- Independent checks to add: re-fetch 5% of chunks from a second provider and compare log sets;
  compare per-pool daily swap counts with a Goldsky or The Graph subgraph; receipt spot-checks
  (200 logs); header cadence on the 2-second grid and empty-block runs (sequencer outages show
  as bursts of empty blocks, not timestamp gaps); parent-hash continuity; fetch only up to the
  finalized tag. Report each with pass/warn/fail thresholds.

Sources: provider documentation pages (many unverified through search excerpts), SQD
eth_getLogs limits, OP Stack derivation spec, Goldsky and The Graph docs.

## Build order

Done so far (September 17):

- Concentrated-liquidity engine: exact v3 math (`venues/clmm`, verified against the v3-core
  spec vectors), `ClPoolState` with the word-boundary tick search, engine replay of
  `cl_init` / `cl_modify` / `cl_swap` rows with every swap a checkpoint, quotes, fills,
  valuation and session views on virtual depth; v4 PoolManager and v3 factory collectors with
  dynamic-fee resolution; real weeks can be requested per venue.
- v2 reconciliation hardened after the first real week came out diagnostic: every Sync is kept
  (orphan Syncs are explained adjustments), multi-input swaps become net reserve adjustments,
  every checkpoint re-anchors the model to the chain, and pools that still do not reconcile
  leave the executable set with the reason on record. Diagnostic weeks from an older collector
  are rebuilt once, automatically.
- v3/v4 reconciliation follows the same policy: a swap the exact loop cannot replay (both legs
  paid in or nothing moved, which is what a hook that absorbed a leg leaves in the log) anchors
  the reference to the recorded after-state as an explained adjustment; any other mismatch is
  material, re-anchors the reference, and the pool leaves the executable set. Demoted pools stay
  in the pack as data (their swaps still price holdings) and the rest of the week qualifies.
  Before a diagnostic week is collected again, its existing pack is revalidated under the
  current validator, so an engine fix that reconciles the same data costs no RPC requests.

- Universe for v3/v4 weeks: established pools plus launches from inside the week, each launch
  discoverable at its 20th swap (no look-ahead beyond the pool's own first swaps). Hooked v4
  pools offer no route in their first two minutes (launch MEV modules are not modelled).

Still open, in order:

1. Clanker hook fee model for agent fills on v4 pools (static per-direction fee, the 20%
   protocol cut, the two-minute MEV module window) so an agent's own fill on a hooked pool
   is priced the way the hook would price it; today the pool fee is the last observed hook fee.
2. Fidelity harness: replay real wallets' recorded trades from a week through the simulator
   and measure fill and PnL error per model. Every later change must reduce that error.
3. Gas from block headers plus per-venue calibrated constants; charged to every fill.
4. Token-behaviour detection and per-pool restriction records; taxed fills and typed sell
   refusals.
5. MEV classifier over the week, measured `p(x)`, arbitrage-reversion step with a fitted
   half-life; both selectable execution models declared in every report.
6. Address-batched fetching, second-provider and subgraph cross-checks in the validation
   report; Aerodrome volatile pools.
