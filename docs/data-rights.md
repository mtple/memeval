# Data rights

Rights are recorded per pack as four independent fields in `manifest.rights`:
`storage_basis`, `local_processing_basis`, `redistribution`, `simulator_serving`.

| Source | Storage | Local processing | Redistribution | Serving |
|---|---|---|---|---|
| Generated fixtures | generated locally | generated locally | permitted (generated) | permitted |
| Report excerpts (`fixtures/reported_evidence`) | supplied by owner | supplied by owner | not cleared | diagnostic only |
| Base public RPC logs (collector) | public chain data via configured endpoint; endpoint terms not reviewed here | research | not cleared | local only |
| CoinGecko / GeckoTerminal (optional collector) | per provider terms, unreviewed | per provider terms | disabled by default | not served |
| GoPlus / DexScreener / Zerion (imports only) | per provider terms, unreviewed | per provider terms | disabled | not served |
| Bankr | optional import of recorded quote evidence only | validation only | disabled | not served |

Rules:

- Rights review is a prerequisite for acquisition, not an assertion that a use is permitted.
  This repository clears no provider's rights.
- Provider-response redistribution is disabled by default. Participant exports never contain
  raw datasets; admin exports contain ledgers and aliases, not provider bodies.
- Collection is opt-in with an explicit request budget and a documented
  `authorization_note`. Default external-spend authorization is zero; a configured credential
  does not itself authorize billable calls.
- Tests and the demo never contact a provider (non-loopback sockets are blocked in tests).
- No prediction-market sources are used.
