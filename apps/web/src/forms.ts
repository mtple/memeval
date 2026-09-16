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
