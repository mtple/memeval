# Continuous week collection

The new continuous episode covers September 7, 2026 at 00:00:00 UTC through September 14 at
00:00:00 UTC, with an exclusive end. The existing September 8–12 daily episodes remain unchanged.
Concatenating those days would lose later activity in pools launched on earlier days. Each
adjacent pair shares only 16 pool keys.

## Collection and validation

`collect-continuous-week.yml` is a separate, manually dispatched GitHub Actions workflow. It
uses the existing `RPC_URL` or `BASE_RPC_URL` repository secret and the existing collectors. Its
universe includes every ETH-paired launch with at least one swap across Uniswap v2, v3, and v4,
plus the existing rule's 16 established pools. It retains every recorded event for those pools
through the end of the week. Unsupported pools and missing coverage stay in the inventory.
There is no sampled fallback or higher minimum trade count.

The workflow does not push commits, touch `record-week`, cancel other workflows, change daily
packs, or serve interactive trading. Its concurrency group applies only to this new workflow
and period, with cancellation disabled. It checks out main and uses a separate work directory
under ignored `data/continuous-week-v1/`. The old weekly cache on `record-week` is not reused.

Each RPC collection slice runs for at most 40 minutes before the existing collector yields at a
checkpoint. Normalization and validation can run after that deadline and have the workflow's
100-minute limit. Automatic continuations stop after eight slices or any failure other than a
clean time-slice checkpoint. No failed or incomplete dataset is published.

The default cap is 40,000 RPC attempts and 16 GiB of received response bodies across every
slice. This matches the existing weekly collector's caps. `rpc-budget.json` records each attempt
before it reaches the provider, including retries and transport failures. Received response
bytes are charged before a response reaches the collector. A single response can cross the byte
cap; the job then stops and cannot send another request. The same private ledger is restored
for the next slice. Resuming with different caps or a missing ledger fails instead of resetting
usage. A runner lost before saving its cache requires an operator review of provider usage and
the last saved ledger; do not start a fresh job to hide uncertain usage.

The repository is private. The workflow refuses to collect if that changes, because Actions
caches contain raw historical data. Only the small measurement report is uploaded as an Actions
artifact. Participating agents never receive cache paths, raw files, or historical storage keys
through the agent API.

For an authorized offline reproduction using the existing environment variable:

```sh
uv run python scripts/collect_continuous_week.py \
  --start 2026-09-07 --end 2026-09-14 \
  --work-root data/continuous-week-v1/2026-09-07_2026-09-14 \
  --max-requests 40000 --max-minutes 40
```

Exit code 3 means the collector saved a resumable slice. Exit code 0 means exact-boundary
validation and indexed export passed. Other exits require inspection of the private work directory and never
trigger an automatic retry. Use the same arguments and directory when resuming.

## Exact UTC boundaries

The existing collectors anchor periods to the first block at or after the requested boundary.
The committed daily packs start at 00:00:01 UTC. The helper preserves that original collection
output and creates a separate weekly pack with exact requested UTC boundaries.

It reads the block immediately before and at or after each boundary through the same budgeted
RPC client. The four headers must bracket the requested timestamps and agree with the checked
two-second Base interval. Every tape row must agree with that block timeline and precede the
exclusive end. Any disagreement blocks publication; the helper does not filter, shift, or
downsample event rows.

`utc-boundaries.json` contains the proof, source pack identity, and source manifest hash. The
helper adds that file to the manifest's hashed objects and computes a new pack identity. It then
runs all qualification gates on the resulting weekly pack. Daily manifests and event files are
never rewritten.

## Measurements and delivery

`collection-profile.json` reports each slice's elapsed time, cumulative requests and bytes,
peak process resident memory, validation duration and gates, pool and asset counts, event counts
by kind, and the size and SHA-256 hash of every finished pack file. It distinguishes completed
measurements from an unfinished collection.

The five existing days contain 1,062,609 normalized events, 638,403,401 uncompressed tape bytes,
and 98,982,043 gzip tape bytes. The prior partial full-week job recorded 1,626,614 raw logs and
logged 927 MiB peak resident memory during collection. The full-week event count and final size
must come from the new completed artifact, not an extrapolation from those partial recordings.

The helper exports every normalized row into deterministic gzip chunks, bounded by 10,000 events
and 4 MiB of decoded JSON each. It retains complete equal-time event groups and refuses an
oversized group rather than splitting or dropping it. The index includes source hashes,
normalized tape hashes, per-chunk hashes, event positions, and block and time ranges. Pack
metadata includes the manifest, qualification report, coverage, inventory, and UTC proof.

The destination for the immutable validated week is private Supabase Storage in the existing
project. Set `publish` only after confirming the project's remaining storage allowance. The
default upload cap is 750,000,000 bytes, leaving space within the existing 1 GB allowance for
checkpoints when the project is otherwise empty. The job measures the full indexed export
before uploading. It will not change a plan or discard events to fit the cap.

Publishing needs `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` or `SUPABASE_SECRET_KEY` in
Actions, using the existing project's server-only credentials. The workflow does not obtain
credentials from Vercel or provision another project. `--publish` uses the same helper path for
an authorized offline invocation. Uploads go into the private `market-replay-private` bucket
under a content-addressed `datasets/{pack_id}/` prefix. An existing object must match its hash;
it is never overwritten. The index is uploaded after all of its objects.

On success, `catalog-candidate.json` contains only the small storage descriptor and episode
name. The workflow retains it as a private review artifact and does not write `weeks/catalog/`
or register an episode. First verify complete replay and checkpoint recovery on that exact pack
and record the runtime measurements. Then integrate only the candidate descriptor into
`weeks/catalog/base_week_2026-09-07.json` using a normal main-branch commit. Re-fetch main before
the commit and preserve other agents' changes. Raw tapes, chunks, work directories, and secrets
must never enter that commit.
