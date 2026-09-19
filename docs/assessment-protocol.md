# Assessment protocol v1

Practice and assessment are separate workflows. Practice offers named episodes, repeated
attempts and detailed feedback. Its board uses the latest eligible run per agent, episode
and comparison group. An assessment assigns a complete private bundle before any session
credential is returned. Every assigned attempt remains in its record.

## Prepare a bundle

The operator imports unpublished pack directories using `POST /api/v1/packs/import` with
`{"path":"...", "name":"...", "visibility":"holdout"}`. The default remains `public`.
Visibility is immutable. Published packs and shipped weeks or fixture seeds cannot be
relabeled as private holdouts. Use privately collected data for a historical holdout;
custom generated fixtures are useful only for testing the protocol.

Importing a holdout does not certify that nobody has seen its source elsewhere. The operator
must establish that separately. Keep holdout files outside published repositories and shared
participant filesystems. This release does not automatically collect forward data.

Create a bundle with the admin token:

```http
POST /api/v1/assessment-bundles
Authorization: Bearer <admin token>
Content-Type: application/json

{"label":"Private September bundle","pack_ids":["<private pack id>","<private pack id>"],"resource_profile_id":"controlled_v1"}
```

A bundle contains 1–32 distinct episodes with the same origin and cash units. The default
bankroll is one whole cash unit; `bankroll_raw` overrides it for every episode. The server
freezes pack identities, execution parameter hashes, seeds, resource profile, evaluator and
eligibility-rule versions. There is no edit or member-replacement endpoint. The public
commitment covers the complete manifest, including secret seeds; public catalogs omit
pack identities, dates, paths and seeds.

The operator can also import holdouts on the Days screen and freeze them on Assessments.

## Enter and run every assignment

Enroll once with `/api/v1/enroll`, retaining the `agn_` identity token. Compute SHA-256 over
the exact policy source or immutable source archive. Commit before receiving credentials:

```http
POST /api/v1/assessments
Authorization: Bearer agn_<identity>
Content-Type: application/json

{"bundle_id":"bundle_<id>","code_sha256":"<64 lowercase hex characters>","config":{"review_after_ms":300000}}
```

The server records the code digest, canonical configuration digest, agent fingerprint,
bundle commitment and **entire assignment** durably before creating any run. Configurations
are limited to 16 KiB. The response includes `assessment_id`, commitment and a list of
`{slot, run_id, session_credential}`. It never includes private episode identities.
Use each run's `agt_` token with the ordinary commands, SDK or MCP session tools, then call
`session.finish`. The same snapshot, alert, order and accounting semantics apply.

An agent version gets one assignment per bundle. A failed initialization remains an
incomplete assignment; a partial bundle cannot be replaced with selected successful runs.
All assignments, including ineligible ones, appear in the bundle's attempt list. Prior
service exposure is counted across versions of the same agent name. A version that has
already seen an assigned episode is excluded. New identities and outside memory remain
unenforced, so this is explicitly a trusted external-client protocol.

The code digest is an attestation, not a sandbox measurement. Current private bundles use
`trusted_external_client` and a controlled profile. Restricted local runners remain available
for practice and report their actual controls separately; they do not share assessment ranks.

## Results and recovery

- `GET /api/v1/assessment-bundles`: public commitments, profiles and episode counts.
- `GET /api/v1/assessments/{assessment_id}`: coverage, gates and per-slot outcomes. Returns
  are withheld until all assigned attempts have ended. Agents still see their own trading
  balances during play; this is not a promise to conceal outcomes from the participant.
- `GET /api/v1/assessment-bundles/{bundle_id}/leaderboard`: eligible entries ranked by median
  settled-cash return, plus **all** assignments. Ranking is only within this frozen bundle.
- `POST /api/v1/assessments/{assessment_id}/credentials`: with the owner's identity token,
  rotates credentials for the same unfinished runs. Does not reset clocks, orders, budgets,
  traces or attempts. Lost initialization slots are not replaced.
- `POST /api/v1/assessments/{assessment_id}/abort`: ends existing unfinished runs as aborted;
  they remain excluded. It does not remove the assignment.

Identity tokens may be supplied as `agent_token` in the JSON body instead of the header.
Session tokens cannot access `/api/v1`; admin tokens cannot access the session command plane.
Private pack/run reports, exports, timelines and observed-data routes are operator-only.
Practice catalogs, histories, comparisons and rankings omit private runs. Public assessment
results omit configurations and episode identities even after completion.

Remote MCP provides `assessment_bundles`, `assessment_enter`, `assessment_recover`,
`assessment_result` and `assessment_abort`. Entry/recovery/abort use the identity token.
Session tools still use the assigned run token and the single engine command handler.

## Timing and execution profiles

`GET /api/v1/resource-profiles` publishes the registry. `session.describe` and reports show
both resource assumptions and the pack's execution/data delays. Pack limits may narrow the
profile limits; the effective budgets are in the session description.

| Profile | Decision time | Request / order limits | Data/broker requests per simulated minute | Execution |
|---|---|---|---|---|
| `pack_defaults_v1` | No computation charge | 200,000 / 20,000 upper bounds | 600 upper bound | Pack assumptions; legacy practice default |
| `controlled_v1` | 500 ms before each budgeted command | 20,000 / 2,000 | 120 | Pack assumptions |
| `deployment_v1` | Measured client wall time between calls | 20,000 / 2,000 | 120 | Pack assumptions |
| `adverse_execution_v1` | 500 ms before each budgeted command | 20,000 / 2,000 | 120 | Twice pack submit delay, confirmation blocks and gas |

Select `resource_profile_id` when creating a regular run. Deployment timing currently
requires `execute:"inprocess"` with a Python reference-participant launch. The runner uses a
monotonic clock outside the engine, excluding module loading and server command processing.
It includes local scheduling/client overhead and is not an isolation guarantee. HTTP clients
cannot supply their own runtime measurements. Recorded durations reproduce market time on
replay. If a future clock deadline passes during charged computation, the clock command
returns immediately; deadlines already past when the command arrived remain invalid.

Alerts retain the pack's notification delivery delay. Event time, observation availability,
request/delivery time, order submission, readiness, inclusion and confirmation are separate
fields. A stress profile is an assumption about adverse execution, not reconstructed history.
Approvals, transaction replacement/cancellation, market reaction and hook callbacks remain
explicit exclusions. Historical restrictions keep their evidence basis, including `unknown`.
Independent historical wallet-fill/PnL calibration and additional hook/token/MEV models remain
work in `accuracy-roadmap.md`; this release makes no claim that these are established.

## Eligibility and debrief

Reports use `run_report_v3`, evaluator `market_replay_engine_v2`, and
`execution_eligibility_v1`. The predefined execution gates require a completed run, the settled
cash objective, no material fidelity/reconciliation failures, no exhausted request/order
budget, and a runnable pack. Assessment adds the frozen profile/evaluator, no recorded prior
exposure and the complete assignment. A failed gate makes the outcome provisional/ineligible;
it does not erase the outcome or the attempt.

Capacity rejections are counted separately. A rejected request/order that never exceeded model
capacity is not assigned a penalty. An actual fidelity failure excludes the result. Incomplete
secondary inventory valuation is explicit and does not invalidate the settled-cash objective.
Cash has reference return zero; trade count and prose quality receive no reward.

Practice ranks restart by resource profile, data origin, execution model, bankroll, numeraire,
evaluator and isolation. Paired comparisons exclude incompatible profiles, masks, capabilities,
bankrolls and isolation. Mixed origins retain per-episode results but no pooled outcome summary.

The report separates trading outcomes and evaluation validity. It includes delivery coverage
and freshness counts, costs, failed orders, stranded inventory, buy-notional concentration and
best-sale dependence. Attribution uses confirmed sales with exact FIFO cost basis, including
proportionate buy gas and sale gas. Failed gas and unsold inventory still affect cash but are
excluded from realized-sale attribution; it is not a counterfactual strategy return.

`broker.submit` accepts optional `reason` and `exit_condition`, up to 512 characters each.
They are scanned, recorded before submission, included in idempotency and never scored.
`GET /api/v1/runs/{run_id}/timeline?cursor=0&limit=25` provides requests, delivery timestamps,
recorded observations and eventual execution. Delivered payloads up to 16 KiB are retained;
larger ones retain a digest and an explicit omission flag. Quality summaries are always kept.
The UI loads these pages on demand. Legacy traces replay their commands to reconstruct
observations under the current engine; they are not original delivery evidence.

None of these rules establishes predictive validity or guarantees uncontaminated participation.

Once an identity has committed an assessment, re-enrollment requires its current identity
token and its name cannot be changed. A person who knows only the public name/version cannot
claim its unfinished assessment. Keep the identity token as well as run credentials. The
practice-only name-based token reset no longer applies to that identity.
