/** Control-plane client and shared types. All paths are under <server>/api/v1; every list endpoint returns {items}. */

export const TOKEN_KEY = "mr_admin_token";
export const SERVER_KEY = "mr_server_url";

function storageGet(k: string): string {
  try {
    return localStorage.getItem(k) ?? "";
  } catch {
    return "";
  }
}

function storageSet(k: string, v: string): void {
  try {
    if (v) localStorage.setItem(k, v);
    else localStorage.removeItem(k);
  } catch {
    /* storage unavailable: value lives only in memory for this page */
  }
}

let memoryToken = storageGet(TOKEN_KEY);
let memoryServer = storageGet(SERVER_KEY);

export function getToken(): string {
  return memoryToken || storageGet(TOKEN_KEY);
}

export function setToken(t: string): void {
  storageSet(TOKEN_KEY, t);
  memoryToken = t;
}

/** Base URL of the Market Replay server ("" = same origin, e.g. when the FastAPI app serves this UI). */
export function getServerUrl(): string {
  const v = memoryServer || storageGet(SERVER_KEY) || (import.meta.env?.VITE_API_BASE as string | undefined) || "";
  return v.replace(/\/+$/, "");
}

export function setServerUrl(url: string): void {
  const v = url.trim().replace(/\/+$/, "");
  storageSet(SERVER_KEY, v);
  memoryServer = v;
}

export function resolveApiBase(): string {
  return `${getServerUrl()}/api/v1`;
}

/** Why a request failed. The UI shows one message per kind instead of a raw status code. */
export type FailureKind = "no_backend" | "unreachable" | "auth" | "api" | "server";

export class ApiError extends Error {
  status: number;
  body: unknown;
  kind: FailureKind;
  constructor(status: number, message: string, body: unknown, kind: FailureKind) {
    super(message);
    this.status = status;
    this.body = body;
    this.kind = kind;
  }
  get isAuth(): boolean {
    return this.kind === "auth";
  }
}

const NO_BACKEND_MSG = "No Market Replay server answered at this address. This page is only the interface; it needs a running server behind it.";

export async function api<T>(path: string, init: RequestInit = {}, auth = true): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (init.body) headers["Content-Type"] = "application/json";
  const tok = getToken();
  if (auth && tok) headers.Authorization = `Bearer ${tok}`;
  let res: Response;
  try {
    res = await fetch(`${resolveApiBase()}${path}`, { ...init, headers: { ...headers, ...(init.headers as Record<string, string> | undefined) } });
  } catch (e) {
    throw new ApiError(0, `Could not reach ${getServerUrl() || "this origin"}: ${(e as Error).message}. Is the server running and is this origin allowed (MARKET_REPLAY_CORS_ORIGINS)?`, null, "unreachable");
  }
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) {
    const isJson = body !== null && typeof body === "object";
    if (res.status === 401 || res.status === 403) {
      const detail = isJson && "detail" in (body as object) ? (body as { detail: unknown }).detail : body;
      const msg = typeof detail === "string" ? detail : detail && typeof detail === "object" && "message" in detail ? String((detail as { message: unknown }).message) : "not authorised";
      throw new ApiError(res.status, msg, body, "auth");
    }
    if (!isJson && res.status >= 500) {
      // The server exists but failed (a cold start, a database that went away, a crash).
      throw new ApiError(res.status, `The server failed to answer this request (HTTP ${res.status}). Try again in a moment; if it keeps happening the operator can read the cause in the server logs.`, body, "server");
    }
    if (!isJson) {
      // A static host (or a non-API server) answered: there is no backend behind this address.
      throw new ApiError(res.status, NO_BACKEND_MSG, body, "no_backend");
    }
    const b = body as Record<string, unknown>;
    const detail = "message" in b ? b.message : "detail" in b ? b.detail : body;
    const msg = typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, msg, body, "api");
  }
  return body as T;
}

export const get = <T>(p: string) => api<T>(p);
export const post = <T>(p: string, body?: unknown) => api<T>(p, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const list = async <T>(p: string): Promise<T[]> => (await api<{ items: T[] }>(p)).items ?? [];

export const ISOLATIONS = ["trusted_external_client", "restricted_local_runner"] as const;
export const MODES = ["practice", "sealed"] as const;

// ------------------------------------------------------------------ types

export type Gate = { gate: string; status: "passed" | "failed" | "warning" | "not_applicable" | string; detail?: string; [k: string]: unknown };

export type LatencyAssumptions = {
  profile_name: string;
  label: string;
  block_interval_ms: number;
  data_latency_ms: number;
  quote_latency_ms: number;
  submit_latency_ms: number;
  confirm_blocks: number;
  quote_ttl_ms: number;
  availability_delay_ms: number;
  gas_cost_raw: string;
  gas_basis: string;
  capacity_profile: { version: string; max_input_bps_of_reserve: number; max_cumulative_displacement_bps: number; note: string };
  settlement_tail_blocks: number;
  is_measured: boolean;
};

export type Universe = {
  factories: string[];
  pool_models: string[];
  quote_asset: string;
  selection_rule_version: string;
  candidate_count: number;
  selected_count: number;
  unsupported_count: number;
  missing_count: number;
  description: string;
};

export type Rights = { storage_basis: string; local_processing_basis: string; redistribution: string; simulator_serving: string; notes: string[] | string };

export type Pack = {
  pack_id: string;
  episode_id: string;
  name: string;
  origin: string;
  chain: string;
  scope_label: string;
  use_status: string;
  duration_ms: number;
  is_full_week: boolean;
  execution_model: string;
  imported_at: string;
  runnable: boolean;
  diagnostic_only: boolean;
  supported_actions: string[];
  unsupported_capabilities: string[];
  predictive_validity: string;
  period_dev_mode?: { start_utc: string; end_utc: string; note: string } | null;
  period?: { start_utc: string; end_utc: string } | null;
  label?: string;
  kind?: "real" | "practice";
  summary: {
    pools_total: number;
    pools_executable: number;
    assets_total: number;
    tape_events: number;
    coverage_states: Record<string, number>;
    gates: Gate[];
    executable_failure: string | null;
    numeraire_alias: string;
    numeraire_decimals: number;
    token_behavior_basis: string;
    availability_basis: string;
    prehistory_ms: number;
    latency_assumptions: LatencyAssumptions;
    universe: Universe;
    rights: Rights;
    unsupported_inventory: { pool: string; reason: string }[];
    provenance_notes: string[];
    scenario: string | null;
  };
};

export type Validation = {
  validator_version: string;
  gates: Gate[];
  executable_failure: string | null;
  requested_qualification: string;
  resulting_qualification: string;
  predictive_validity: string;
  notes: string[];
};

export type PackHealth = {
  pack: Pack;
  universe: Universe;
  ingestion: { tape_events: number; blocks_table_rows: number; indexed_block_ranges: unknown[] };
  coverage: {
    intervals: number;
    non_complete_intervals: { object_ref: string; field: string; start_utc_ms: number; end_utc_ms: number; state: string; evidence: string; gaps: unknown }[];
    non_complete_count: number;
  };
  duplicates_suspected: unknown[] | number;
  unpublished_observations: unknown[] | number;
  corrections: unknown[];
  missing_state_pools: string[];
  unsupported_inventory: { pool: string; reason: string; model?: string }[];
  missing_inventory: unknown[];
  source_disagreements: unknown[];
  restriction_observations: number;
  token_behavior_basis: string;
  rights: Rights;
  validation: Validation;
  attempts: { pack_id: string; agent_id: string; count: number }[];
  exposed_runs: number;
  decision_log: string[];
};

export type Agent = {
  agent_id: string;
  name: string;
  version: string;
  runtime: string;
  fingerprint: string;
  capabilities: string[];
  config: Record<string, unknown>;
  created_at: string;
  compatibility: { compatible: boolean; unsupported_requested: string[] };
};

export type Suite = {
  suite_id: string;
  description: string;
  packs: string[];
  pack_count: number;
  resolved_pack_ids: string[];
  bankroll_raw: string;
  mode: string;
  sealed: boolean;
  isolation: string;
  fingerprint: string;
  notes: string[] | string;
  all_packs_imported: boolean;
};

export type Role = "admin" | "public";
export type Meta = {
  role: Role;
  public_runs: boolean;
  rate_limit_per_hour_per_ip?: number;
  tools: Record<string, string>;
  unsupported_capabilities: string[];
  gateway_url: string;
  mcp_url?: string;
  dev_mode: boolean;
  hosted?: boolean;
  runtimes_available?: string[];
  store_backend?: string;
  weeks_enabled?: boolean;
};
export type WeekJob = {
  job_id: string;
  name: string;
  label: string;
  chain: string;
  protocol?: string;
  duration_hours: number;
  dates_sealed: boolean;
  period_start_utc?: string;
  period_end_utc?: string;
  status: "queued" | "collecting" | "built" | "failed" | string;
  requests_used: number;
  request_budget: number;
  attempts: number;
  note: string | null;
  error: string | null;
  pack_id: string | null;
  qualification?: string | null;
  created_at: string;
  updated_at: string;
};
export type Usage = { today: { runs: number; cpu_seconds: number }; month: { runs: number; cpu_seconds: number }; caps: { max_runs_per_day: number; max_cpu_seconds_per_month: number; max_runs_per_hour_per_ip?: number }; remaining: { runs_today: number; cpu_seconds_month: number }; note: string };

/** The few numbers a result list needs; null until the run has a report. */
export type ResultSummary = {
  headline_return: string | null;
  valuation_complete: boolean;
  numeraire: string | null;
  numeraire_decimals: number | null;
  initial_equity_raw: string | null;
  terminal_model_equity_raw: string | null;
  max_drawdown: string | number | null;
  confirmed_fills: number;
  orders_total: number;
  gas_total_raw: string | null;
  unpriced_inventory: number;
  unresolved_orders: number;
  status_dimensions: Record<string, string>;
};

export type Holding = { asset_id: string; class: "priced_liquidatable" | "no_route" | "unpriced_missing_data" | string; quantity_raw: string; model_value_raw: string | null };

export type RunState = "queued" | "running" | "paused" | "completed" | "agent_failed" | "environment_failed" | "budget_exhausted" | "aborted";

export type Run = {
  run_id: string;
  agent_id: string;
  agent_name: string | null;
  agent_version: string | null;
  pack_id: string;
  episode_id: string;
  pack_name: string;
  pack_label?: string | null;
  suite_id: string | null;
  suite_run_id: string | null;
  mode: string;
  isolation: string;
  state: RunState | string;
  bankroll_raw: string;
  profile_hash: string;
  launch: unknown;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  clock_ms: number;
  exposed: boolean;
  has_report: boolean;
  result_summary: ResultSummary | null;
  live?: {
    clock_ms: number;
    remaining_ms: number;
    duration_ms: number;
    requests: number;
    decisions: number;
    orders_total: number;
    pending_orders: number;
    cash_available_raw: string;
    cash_reserved_raw: string;
    model_equity_raw: string | null;
    valuation_complete: boolean;
    holdings: Holding[];
    recent_actions: { tool: string; status: string; error_code: string | null; clock_ms: number }[];
    fidelity_flags: string[];
    data_health: { rate_limited: number | boolean; invalid_calls: number | boolean; budget_exhausted: boolean };
    isolation_controls: Record<string, unknown>;
  } | null;
  session_credential?: { token: string; gateway_url: string; commands_url: string; mcp_url: string } | null;
};

export type Bar = {
  start_ms: number;
  end_ms: number;
  open: string | null;
  high: string | null;
  low: string | null;
  close: string | null;
  volume_base_raw: string;
  volume_quote_raw: string;
  trade_count: number;
  closed: boolean;
  synthetic_empty_bar: boolean;
  completeness: string;
};

export type Observed = {
  clock_ms: number;
  pools: { pool_id: string }[];
  series: { pool_id: string; interval_ms: number; items: Bar[]; gaps: { start_ms: number; end_ms: number; reason: string }[]; as_of_ms: number } | null;
  equity: { time_ms: number; equity_raw: string | null; complete: boolean; source: string }[];
  orders: Record<string, unknown>[];
  note: string;
};

export type Report = {
  report_version: string;
  run: Record<string, unknown>;
  status_dimensions: Record<string, string>;
  outcome: {
    numeraire: string;
    numeraire_decimals: number;
    initial_equity_raw: string;
    terminal_model_equity_raw: string | null;
    valuation_complete: boolean;
    headline_return: string | null;
    return_definition: string;
    terminal_cash_raw: string;
    terminal_priced_inventory_raw: string;
    terminal_liquidation_gas_raw: string;
    valuation_policy: string;
    valuation_warnings: string[];
  };
  risk: {
    max_drawdown: string | number | null;
    drawdown_basis: string;
    equity_points_total: number;
    equity_points_complete: number;
    gaps: unknown[];
    gap_count: number;
    exposure_share_of_grid: number | string | null;
    largest_position: { asset_id: string; share: number | string } | null;
  };
  costs: {
    gas_total_raw: string;
    gas_basis: string;
    implicit_pool_fee_numeraire_raw: string;
    implicit_pool_fee_other_raw: string | Record<string, string>;
    fee_note: string;
    turnover_numeraire_raw: string;
  };
  activity: {
    orders_total: number;
    orders_by_state: Record<string, number>;
    confirmed_fills: number;
    reverted: number;
    expired: number;
    model_capacity_rejected: number;
    tool_calls: Record<string, number>;
    requests_total: number;
    decisions_total: number;
    quality_exposure: { invalid_calls: number; rate_limited: number; errors_by_code: Record<string, number> };
    budget_exhausted: boolean;
  };
  unresolved: {
    orders: Record<string, unknown>[];
    no_route_inventory: { asset_id: string; quantity_raw: string; reason: string }[];
    unpriced_inventory: { asset_id: string; quantity_raw: string; reason?: string }[];
    environment_fidelity_flags: string[];
  };
  coverage_and_assumptions: {
    episode_duration_ms: number;
    is_full_week: boolean;
    universe: Universe;
    latency_assumptions: LatencyAssumptions;
    capacity_profile: LatencyAssumptions["capacity_profile"];
    availability_model: string | Record<string, unknown>;
    reconciliation_mismatches_in_run: number;
    reserve_adjustments_in_run?: Record<string, number>;
    limitations: string[];
  };
  versions: Record<string, string>;
  reproducibility: { ledger_hash: string; state_hash: string; trace_hash: string; result_hash: string; trace_length: number };
  wall_clock: Record<string, unknown>;
  inference: Record<string, unknown>;
  statement: string;
};

export type ReplayResult = { run_id: string; trace_length: number; ledger_matches: boolean; state_matches: boolean; [k: string]: unknown };

export type CompRun = {
  run_id: string;
  state: string;
  headline_return: string | null;
  valuation_complete: boolean;
  max_drawdown: string | number | null;
  gas_total_raw: string | null;
  confirmed_fills: number;
  unresolved_orders: number;
  unpriced_inventory: number;
  error: string | null;
};

export type Comparison = {
  comparison_id: string;
  created_at: string;
  suite_id: string | null;
  suite_fingerprint: string | null;
  agents: { a: Agent; b: Agent };
  per_episode: {
    episode_label: string;
    runs_a: CompRun[];
    runs_b: CompRun[];
    paired: boolean;
    return_diff_a_minus_b: string | number | null;
    gas_diff_a_minus_b_raw: string | null;
    drawdown_diff_a_minus_b: string | number | null;
  }[];
  summary: {
    episodes_total: number;
    episodes_paired: number;
    runs_attempted_a: number;
    runs_attempted_b: number;
    runs_not_completed_a: number;
    runs_not_completed_b: number;
    return_diff?: Stats;
    gas_diff_raw?: Record<string, string | number>;
    drawdown_diff?: Stats;
  };
  warnings: string[];
  evidence_counts: { unique_calendar_periods: number; chains: number; stochastic_trials_a: number; stochastic_trials_b: number };
  statement: string;
};

export type Stats = { median: string | number | null; mean: string | number | null; min: string | number | null; max: string | number | null; a_better_count: number; b_better_count: number };

export const EXAMPLES = ["cash_only", "scheduled_basket", "random_actions", "model_client"] as const;
export const RUNTIMES = ["python", "typescript"] as const;

export type LeaderboardCategory = { kind: "suite" | "pack" | "all"; id: string; label: string; description?: string; episodes: string[] };
export type LeaderboardRow = {
  rank: number;
  agent_id: string;
  agent_name: string;
  agent_version: string;
  runtime: string;
  episodes_valued: number;
  episodes_total: number;
  covers_all: boolean;
  runs_attempted: number;
  median_return: string;
  mean_return: string;
  best_return: string;
  worst_return: string;
  worst_drawdown: string | null;
  fills: number;
  last_finished_at: string | null;
  run_ids: string[];
};
export type Leaderboard = { category: LeaderboardCategory; categories: LeaderboardCategory[]; rows: LeaderboardRow[]; note: string };

/** The agent this browser considers "mine" (set from ?agent= on the results link, or typed on the leaderboard). */
export const MY_AGENT_KEY = "mr_my_agent";
export function getMyAgent(): string {
  return storageGet(MY_AGENT_KEY);
}
export function setMyAgent(v: string): void {
  storageSet(MY_AGENT_KEY, v.trim());
}
