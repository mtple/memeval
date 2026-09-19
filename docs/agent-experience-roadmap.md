# Agent experience roadmap: shipped September 2026

The participant feedback led to the following product capabilities. Execution mechanics without sufficient evidence are explicitly excluded, as the
feedback requests. This roadmap is separate from the empirical research in `accuracy-roadmap.md`.

| Feedback | Implementation | Verification |
|---|---|---|
| Trading terminal | `session.snapshot`, client-owned watchlists, paginated discoveries, activity/price/depth/cost hints, freshness and coverage, portfolio/order updates | Terminal timing, pagination, liquidity adapters, leakage and SDK/HTTP/MCP tests |
| Strategy-neutral loop | Starter's `decide` interface, no trades by default; one-shot delayed new-pool, price-cross, liquidity and order-state alerts; review deadlines | Alert latency, no hidden wakeups, deadline boundaries, executable starter |
| Time and attention | Versioned controlled, measured-runner and stress profiles; explicit budgets; recorded runtime; deterministic replay | Computation charges, elapsed deadline handling, server-time exclusion, runner restrictions and recovery |
| Execution failures | Existing expiry/slippage/revert-gas/depth/restriction mechanics; declared exclusions; adverse execution profile kept separate | Existing execution tests plus stress-copy and profile tests |
| Outcomes and validity | Named eligibility gates, provisional outcomes, separate comparison groups, coverage, concentration and FIFO best-sale dependence | Eligibility, attribution, comparison/profile exclusion tests |
| Decision debrief | Bounded delivered observations, request/execution timeline, optional scanned pre-submission reason and exit condition; UI pagination | Metadata idempotency, intent leakage, replay, timeline and browser checks |

See [run-protocol.md](run-protocol.md) for terminology, timing profiles, eligibility rules
and debrief details. See
[agent-integration.md](agent-integration.md) for terminal/alert integration.

Runs use one workflow for recorded and generated episodes. The separate assessment workflow
has been removed. Existing private records remain hidden from public catalogs and results.
Independent wallet-fill/PnL calibration, forward data collection and further hook/token/MEV
mechanics remain empirical follow-up work. Predictive validity is not established.
