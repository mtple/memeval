# Sample artifacts

Small, redistributable outputs copied from an actual `make demo` run and one historical
collection in this environment. Every number was produced by the simulator; nothing is
hand-edited. Large admin export bundles stay in the gitignored `data/` directory.

| File | What it is |
|---|---|
| `demo_summary.json` | suite, agents, run ids/states, wall time, the generated-only statement |
| `demo_comparison.json` | paired comparison `scheduled_basket_python` vs `cash_only_python` on the four generated weeks |
| `participant_export_example.json` | a participant-role export bundle (redacted: no pack id, mask seed, ledger or mappings) |
| `environment_validation.json` | tested / not_tested / failed / assumed items plus six sensitivity runs |
| `historical_slice_validation.json` | validation report of the Base v2 research slice built from the public RPC |
| `historical_slice_manifest.yaml` | its private manifest (dates are real; this pack is `research`, not a week, not committed) |

All generated results are artificial. The historical slice covers 30 minutes of one Base pool
family under stated assumptions and establishes no predictive validity.
