/** Pure builders/validators for control-plane request bodies (mirrors RunBody in service/app.py). */
import { ISOLATIONS, MODES } from "./api";

export type CreateRunForm = {
  agent_id: string;
  pack_id: string;
  mode: string;
  bankroll_raw: string;
  isolation: string;
  launchKind: string; // "example" | "external"
  example: string;
  runtime: string;
  agent_seed: string;
};

export function validateCreateRun(f: CreateRunForm): string[] {
  const problems: string[] = [];
  if (!f.agent_id) problems.push("agent is required");
  if (!f.pack_id) problems.push("pack is required");
  if (!/^\d+$/.test(f.bankroll_raw)) problems.push("bankroll must be a whole number of raw units");
  if (!(ISOLATIONS as readonly string[]).includes(f.isolation)) problems.push(`isolation must be one of ${ISOLATIONS.join(", ")}`);
  if (!(MODES as readonly string[]).includes(f.mode)) problems.push(`mode must be one of ${MODES.join(", ")}`);
  return problems;
}

export function buildCreateRunBody(f: CreateRunForm): Record<string, unknown> {
  const body: Record<string, unknown> = { agent_id: f.agent_id, pack_id: f.pack_id, mode: f.mode, bankroll_raw: f.bankroll_raw, isolation: f.isolation };
  if (f.launchKind === "example") body.launch = { kind: "example", name: f.example, runtime: f.runtime };
  if (f.agent_seed.trim()) body.agent_seed = f.agent_seed.trim();
  return body;
}

/** The one-screen "New run" form: the person's own agent (HTTP or MCP) or a reference participant the server runs. */
export type NewRunForm = {
  driver: "own" | "reference";
  agent_name: string;
  agent_version: string;
  pack_id: string;
  example: string;
  mode: string;
  bankroll_raw: string;
  agent_seed: string;
};

export function validateNewRun(f: NewRunForm): string[] {
  const problems: string[] = [];
  if (f.driver === "own" && !f.agent_name.trim()) problems.push("give your agent a name");
  if (!f.pack_id) problems.push("choose an episode");
  if (!/^\d+$/.test(f.bankroll_raw)) problems.push("bankroll must be a whole number of raw units");
  if (!(MODES as readonly string[]).includes(f.mode)) problems.push(`mode must be one of ${MODES.join(", ")}`);
  return problems;
}

export function buildNewRunBody(f: NewRunForm): Record<string, unknown> {
  const version = f.agent_version.trim() || "1";
  const body: Record<string, unknown> = { pack_id: f.pack_id, mode: f.mode, bankroll_raw: f.bankroll_raw, isolation: "trusted_external_client" };
  if (f.driver === "reference") {
    body.agent = { name: `${f.example}_python`, version, runtime: "python" };
    body.launch = { kind: "example", name: f.example, runtime: "python" };
  } else {
    body.agent = { name: f.agent_name.trim(), version, runtime: "external" };
  }
  if (f.agent_seed.trim()) body.agent_seed = f.agent_seed.trim();
  return body;
}
