"""Read-only external data collectors. Never imported by the engine; never run inside a replay.

All collectors: resumable checkpoints, strict validation, bounded request budgets, backoff,
persisted errors and raw-response preservation with credentials removed.
"""
