# Dataset (pack) format

A pack is an immutable directory:

| File | Content |
|---|---|
| `manifest.yaml` | `EpisodeManifest` (private): `pack_id`, origin, chain, scope label, period (absolute UTC + prehistory), universe, data objects with sha256, execution model + parameters hash, rights, validation, numeraire, generator config, provenance notes, decision log |
| `execution_params.yaml` | `ExecutionParams`: latencies, block interval (fixtures), confirm blocks, quote TTL, availability delay, gas, capacity profile, settlement tail, reporting grid, budgets, rate limit, valuation policy |
| `assets.jsonl` | `Asset`: canonical `chain_id:address`, decimals, metadata, creation/discovery times |
| `pools.jsonl` | `Pool`: canonical `chain_id:protocol:address`, model, assets, fee, creation/discovery, initial reserves + basis, `supported_by_cpmm`, unsupported reason |
| `tape.jsonl` | `TapeEvent` rows ordered by `(block, log_index, seq)`: `swap` (recorded input direction/amount, recorded output), `mint`, `burn`, `sync` (checkpoint), `restriction`, `halt/unhalt`; `time_utc_ms`, optional `publication/received/available_utc_ms`, `availability_basis` |
| `blocks.jsonl` | optional contiguous block→time table (historical packs without a fixed interval) |
| `restrictions.jsonl` | optional `RestrictionObservation` rows (nullable flags, conflicts, availability) |
| `coverage.json` | coverage intervals per pool/field with state `completed_and_checked / partial / missing / failed / pending` and evidence; diagnostics |
| `inventory.json` | unsupported and missing pools with reasons; candidate/selected counts |
| `validation.json` | qualification gate report |

`pack_id = sha256(sorted object hashes + params hash)`. Loading verifies every hash; a modified
byte makes the pack unloadable. The public descriptor (`PublicDescriptor`) is a separately
constructed allowlist: episode id (random, not content-addressed), origin, duration, prehistory,
numeraire alias/decimals, execution model, token behavior, availability basis, latency
assumptions, capabilities, limitations, use status, predictive validity. No dates, paths,
addresses or pack ids.

## Time fields

`event_time` (block time), `publication_time`, `received_at` (collector acquisition),
`available_at` (when agents may see it), `availability_basis`. A later retrieval never
overwrites the acquisition time; historical packs keep `received_utc_ms` after the event and
derive `available_utc_ms` from an explicit delay model.

## Status dimensions

Data origin (`generated_fixture | captured_observations | historical_reconstruction |
report_excerpt | mixed`), availability basis, execution model (`cpmm_fixed_flow_v1 |
diagnostic_no_execution`), token behavior (`known_fixture_rules | historically_supported |
assumed_standard_transfer | unknown`), universe, isolation, use status (`demo | research |
qualified_for_named_suite | diagnostic_only | rejected`), predictive validity
(`not_established`). They are never compressed into one badge.

## Qualification gates (validator `pack_validator_v1`)

1. identity, 2. universe, 3. temporal ordering, 4. trade coverage, 5. execution state
(no-agent reconciliation against `sync` checkpoints in exact integers), 6. mechanics,
7. valuation, 8. anonymization, 9. provenance and rights, 10. reproducibility.

An executable failure downgrades generated packs to `rejected` and historical packs to
`diagnostic_only`. Packs with `diagnostic_no_execution` can never be above `diagnostic_only`.
Passing never upgrades a pack above the requested qualification and never establishes
predictive validity.

## Universe construction

Defined before measuring performance: factories, pool models, quote asset, selection rule
version, indexed block ranges; candidate / selected / unsupported / missing counts reported
separately. Inactive, failed, drained and incomplete pools stay in the inventory. Static venue
support is declared at discovery; later restriction/coverage changes become visible only at
their own availability time (no future qualification leaks).

## Generated fixtures

`fixture_generator_v1` builds deterministic packs from stable keys (seed, pool, block, index).
Shipped suite: `gen_week_trending`, `gen_week_reversal`, `gen_week_sparse_missing` (dropout
windows → coverage `partial`, unpublished observations), `gen_week_liquidity_shift` (mint/burn,
drained + halted pool, fixture sell-block restriction, an unsupported v3-like pool kept in the
inventory), and the two-hour `gen_dev_short` committed under `fixtures/generated/`. Symbols
collide on purpose (two `MOON`s). Everything is labeled `generated_fixture`; nothing is
historical.

## Report-excerpt fixtures

`fixtures/reported_evidence/report_excerpts.json` (`origin=report_excerpt`) wraps the supplied
evidence excerpts. `market-replay import-report-fixtures` builds a `diagnostic_only` pack with
no execution model, an unsupported pool with unknown protocol/reserves, one `partial` trade
page interval, a `missing` candle bucket, and a restriction observation with unknown taxes and
a preserved provider conflict. Its hash is the hash of the excerpt fixture, not of any
provider receipt.
