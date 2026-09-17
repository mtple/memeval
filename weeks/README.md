# Recorded weeks

One directory per real week of Base trading, each an immutable pack (see
`docs/dataset-format.md`). The deployed site registers every directory here on its first
request; these are the only real weeks it offers.

Add a week from your own machine:

```bash
export BASE_RPC_URL=https://...      # your read-only Base endpoint
make week START=2026-09-07           # about two hours
git add weeks/base_week_2026-09-07 && git commit -m "Base week of 2026-09-07" && git push
```

A recording in progress keeps its working files in `weeks/<name>_work/`, which git ignores.
