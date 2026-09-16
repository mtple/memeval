/** Plain-English wording for report status values and summaries. Pure functions; tested in explain.test.ts. */
import { fmtRaw } from "./format";

const SENTENCES: Record<string, Record<string, string>> = {
  data_origin: {
    generated_fixture: "The market was an artificial market: a generated fixture with known rules, not real history.",
    onchain_recorded: "The market was rebuilt from recorded on-chain swaps for a bounded period.",
    provider_snapshot: "The market came from a data provider's snapshots, which are not a complete market.",
  },
  availability_basis: {
    fixture_delay_model: "Prices and trades reached the agent after a modeled delay, never instantly.",
    reconstructed_delay_model: "Prices and trades reached the agent after a delay reconstructed from the source data.",
  },
  execution_model: {
    cpmm_fixed_flow_v1: "Trades were filled by a constant-product pool model with declared latency and capacity limits, not by a real exchange.",
  },
  token_behavior: {
    known_fixture_rules: "Token behaviour (taxes, halts, limits) followed the fixture's known rules.",
    observed: "Token behaviour was taken from what could be observed; unobserved behaviour is unknown.",
    unknown: "Token behaviour was unknown; restrictions may have existed that the model did not apply.",
  },
  isolation: {
    trusted_external_client: "The agent ran outside this server, so nothing prevented it from looking anything up elsewhere.",
    restricted_local_runner: "The agent ran in a restricted local runner with its environment scrubbed.",
    in_process_reference_participant: "A reference participant ran inside the server itself.",
  },
  use_status: {
    demo: "This episode is a demonstration fixture, not a benchmark suite.",
    research: "This episode qualified for research use.",
    qualified_for_named_suite: "This episode qualified for a named benchmark suite.",
    diagnostic_only: "This episode is diagnostic only; its results are not comparable.",
    rejected: "This episode failed validation; treat its results as invalid.",
  },
  predictive_validity: {
    not_established: "This does not predict live results; predictive validity is not established.",
  },
};

const LABELS: Record<string, string> = {
  data_origin: "Market",
  availability_basis: "What the agent could see",
  execution_model: "How trades were filled",
  token_behavior: "Token behaviour",
  isolation: "Where the agent ran",
  use_status: "Episode status",
  predictive_validity: "Predictive validity",
};

export const DIMENSION_ORDER = ["data_origin", "availability_basis", "execution_model", "token_behavior", "isolation", "use_status", "predictive_validity"] as const;

export function dimensionLabel(key: string): string {
  return LABELS[key] ?? key.replace(/_/g, " ");
}

export function explainDimension(key: string, value: string | null | undefined): string {
  const known = value ? SENTENCES[key]?.[value] : undefined;
  if (known) return known;
  const words = (value ?? "unknown").replace(/_/g, " ");
  return `${dimensionLabel(key)}: ${words}.`;
}

export type SummaryInput = {
  agent: string;
  episode: string;
  durationMs: number;
  isFullWeek: boolean;
  unit: string;
  decimals: number;
  initialRaw: string;
  terminalRaw: string | null;
  headlineReturn: string | number | null;
  valuationComplete: boolean;
  orders: number;
  fills: number;
  gasRaw: string | null;
  unpriced?: number;
};

function pct(v: string | number): string {
  const n = Number(v) * 100;
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}

function duration(ms: number, full: boolean): string {
  if (full) return "a full week";
  const h = ms / 3_600_000;
  if (h >= 48) return `${(h / 24).toFixed(1)} days`;
  if (h >= 1) return `${h % 1 === 0 ? h : h.toFixed(1)} hours`;
  return `${Math.round(ms / 60_000)} minutes`;
}

export function summarySentence(i: SummaryInput): string {
  const amt = (raw: string | null) => `${fmtRaw(raw, i.decimals)} ${i.unit}`;
  const start = `${i.agent} traded ${i.episode} (${duration(i.durationMs, i.isFullWeek)}) and started with ${amt(i.initialRaw)}.`;
  let outcome: string;
  if (!i.valuationComplete || i.headlineReturn === null || i.headlineReturn === undefined || i.terminalRaw === null) {
    outcome = `Its final holdings could not be valued${i.unpriced ? ` (${i.unpriced} holding${i.unpriced === 1 ? "" : "s"} had no price)` : ""}, so no return is stated.`;
  } else {
    outcome = `It ended with ${amt(i.terminalRaw)}: ${pct(i.headlineReturn)} after modeled costs.`;
  }
  const activity = i.orders === 0 ? "It never placed an order." : `${i.fills} of ${i.orders} orders filled${i.gasRaw && i.gasRaw !== "0" ? `, costing ${amt(i.gasRaw)} in gas` : ""}.`;
  return `${start} ${outcome} ${activity}`;
}

/** Three short tags for a result card: market, visibility, valuation. */
export function trustTags(dims: Record<string, string | undefined>, valuationComplete: boolean): string[] {
  const market = dims.data_origin === "generated_fixture" ? "artificial market" : dims.data_origin === "onchain_recorded" ? "recorded market" : dims.data_origin ? dims.data_origin.replace(/_/g, " ") : "market unknown";
  const see = dims.availability_basis?.includes("delay") ? "delayed prices" : dims.availability_basis ? dims.availability_basis.replace(/_/g, " ") : "visibility unknown";
  return [market, see, valuationComplete ? "fully valued" : "not fully valued"];
}
