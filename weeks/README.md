# Recorded days

One directory per real period of Base trading, each an immutable pack (see
docs/how-a-real-week-is-built.md). The server registers every directory here on its first
request; these are the only real episodes it offers. The unit that ships is one UTC day
(`base_day_YYYY-MM-DD`): every pool launched on Uniswap v2, v3 and v4 that day with an ETH leg,
plus a few established pools. A whole week of launches is millions of events and does not fit
the hosted function; `base_week_*` directories are either the older sampled universe or weeks
recorded for a self-hosted server.

Add a day from your own machine (about ten minutes) or with the collect-week workflow (push a
commit whose message is `day 2026-09-08` to the `record-week` branch):

```
export BASE_RPC_URL=...                 # a read-only Base RPC endpoint
make day START=2026-09-08
git add weeks/base_day_2026-09-08 && git commit -m "Base day of 2026-09-08" && git push
```

A recording in progress keeps its working files in `weeks/<name>_work/`, which git ignores.

Each day also carries `market_baseline.json`, what a naive fixed stake in every pool would have
returned that day, written by the build and regenerated with `market-replay packs baseline
weeks/<name>` (it is not part of the pack hash, so the pack id does not change).
