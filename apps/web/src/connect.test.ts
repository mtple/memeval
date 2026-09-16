// Seam: what a person copies into their own agent after starting a run. Pure functions, no DOM.
import { describe, expect, it } from "vitest";
import { connectSnippets, type Credential } from "./connect";

const cred: Credential = { token: "agt_abc123", gateway_url: "https://memeval-web.vercel.app", commands_url: "https://memeval-web.vercel.app/agent/v1/commands", mcp_url: "https://memeval-web.vercel.app/agent/mcp" };

describe("connectSnippets", () => {
  const s = connectSnippets(cred);
  it("gives an MCP client config with the bearer header (OpenClaw, Hermes and similar)", () => {
    const cfg = JSON.parse(s.mcpConfig) as { mcpServers: Record<string, { url: string; headers: Record<string, string> }> };
    const server = cfg.mcpServers["market-replay"];
    expect(server.url).toBe(cred.mcp_url);
    expect(server.headers.Authorization).toBe("Bearer agt_abc123");
  });
  it("gives a curl call to the commands endpoint with the token", () => {
    expect(s.curl).toContain(cred.commands_url);
    expect(s.curl).toContain("Bearer agt_abc123");
    expect(s.curl).toContain('"tool": "session.describe"');
  });
  it("gives env exports the SDKs read", () => {
    expect(s.env).toBe("export MARKET_REPLAY_URL=https://memeval-web.vercel.app\nexport MARKET_REPLAY_TOKEN=agt_abc123");
  });
  it("gives a Python SDK snippet that reads those env vars", () => {
    expect(s.python).toContain("client_from_env()");
  });
});
