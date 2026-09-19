# Agent experience roadmap: shipped September 2026

The seven suggestions in the participant feedback are implemented as the following product
capabilities. Execution mechanics without sufficient evidence are explicitly excluded, as the
feedback requests. This roadmap is separate from the empirical research in `accuracy-roadmap.md`.

| Feedback | Implementation | Verification |
|---|---|---|
| Trading terminal | `session.snapshot`, client-owned watchlists, paginated discoveries, activity/price/depth/cost hints, freshness and coverage, portfolio/order updates | Terminal timing, pagination, liquidity adapters, leakage and SDK/HTTP/MCP tests |
| Strategy-neutral loop | Starter's `decide` interface, no trades by default; one-shot delayed new-pool, price-cross, liquidity and order-state alerts; review deadlines | Alert latency, no hidden wakeups, deadline boundaries, executable starter |
| Time and attention | Versioned controlled, measured-runner and stress profiles; explicit budgets; recorded runtime; deterministic replay | Computation charges, elapsed deadline handling, server-time exclusion, runner restrictions and recovery |
| Execution failures | Existing expiry/slippage/revert-gas/depth/restriction mechanics; declared exclusions; adverse execution profile kept separate | Existing execution tests plus stress-copy and profile tests |
| Practice and assessment | Private imports, immutable bundles and commitments, all assigned attempts, no replacements, owner credential recovery, separate UI/API/MCP workflow | Route privacy, complete/incomplete/failed assignment, recovery across instances and prior-exposure tests |
| Outcomes and validity | Named eligibility gates, provisional outcomes, separate comparison groups, coverage, concentration and FIFO best-sale dependence | Eligibility, attribution, comparison/profile exclusion tests |
| Decision debrief | Bounded delivered observations, request/execution timeline, optional scanned pre-submission reason and exit condition; UI pagination | Metadata idempotency, intent leakage, replay, timeline and browser checks |

See [assessment-protocol.md](assessment-protocol.md) for the complete protocol, API calls,
profile values, eligibility rules and assurance limits. See
[agent-integration.md](agent-integration.md) for terminal/alert integration.

Private historical data must still be collected and imported by an operator. The software does
not turn public practice episodes into unseen holdouts. External code/configuration digests are
self-attestations; independent wallet-fill/PnL calibration, forward data collection and further
hook/token/MEV mechanics remain empirical follow-up work. Predictive validity is not established.
