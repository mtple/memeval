/** What a person copies into their own agent after starting a run. Pure string builders, no DOM. */

export type Credential = { token: string; gateway_url: string; commands_url: string; mcp_url: string };

export type Snippets = { env: string; curl: string; mcpConfig: string; python: string; typescript: string };

export function connectSnippets(c: Credential): Snippets {
  const env = `export MARKET_REPLAY_URL=${c.gateway_url}\nexport MARKET_REPLAY_TOKEN=${c.token}`;
  const curl = `curl -sS ${c.commands_url} \\
  -H "Authorization: Bearer ${c.token}" \\
  -H "Content-Type: application/json" \\
  -d '{"request_id": "req_1", "tool": "session.describe", "arguments": {}}'`;
  const mcpConfig = JSON.stringify({ mcpServers: { "market-replay": { url: c.mcp_url, headers: { Authorization: `Bearer ${c.token}` } } } }, null, 2);
  const python = `# pip install -e sdk/python   (package: market_replay_client)
# ${env.replace("\n", "\n# ")}
from market_replay_client import client_from_env

c = client_from_env()
info = c.describe()                      # capabilities, budgets, settlement unit, bankroll
for m in c.all_markets(execution_supported_only=True):
    q = c.quote(m["pool_id"], info["numeraire"]["asset_id"], 1000)
c.advance_next(3_600_000)                # let the market move; all times are relative ms
c.finish()                               # ends the run and produces the report`;
  const typescript = `// node --experimental-strip-types agent.ts
// ${env.replace("\n", "\n// ")}
import { clientFromEnv } from "sdk/typescript/src/index.ts";

const c = clientFromEnv();
const info = await c.describe();
await c.advance(6 * 3_600_000);
await c.finish();`;
  return { env, curl, mcpConfig, python, typescript };
}
