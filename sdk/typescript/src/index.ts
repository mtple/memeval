/**
 * Thin TypeScript client for the Market Replay agent plane.
 *
 * Uses only erasable TypeScript syntax so it runs directly under
 * `node --experimental-strip-types` without a build step. Raw quantities are
 * strings (never JavaScript numbers) and are converted with BigInt.
 */

export type Envelope = {
  request_id: string;
  session_id: string;
  clock_ms: number;
  status: "ok" | "error";
  data: Record<string, unknown> | null;
  quality: {
    completeness: string;
    availability_basis: string;
    observed_through_ms: number | null;
    stale: boolean;
    warnings: string[];
  };
  error: { code: string; message: string; details: Record<string, unknown> } | null;
};

export class CommandError extends Error {
  envelope: Envelope;
  code: string;
  constructor(envelope: Envelope) {
    super(`${envelope.error?.code}: ${envelope.error?.message}`);
    this.envelope = envelope;
    this.code = envelope.error?.code ?? "UNKNOWN";
  }
}

export class MarketReplayClient {
  baseUrl: string;
  token: string;
  sessionId: string | null = null;
  clockMs = 0;
  private counter = 0;

  constructor(baseUrl: string, token: string) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.token = token;
  }

  async call(tool: string, args: Record<string, unknown> = {}, requestId?: string): Promise<Envelope> {
    this.counter += 1;
    const body: Record<string, unknown> = {
      request_id: requestId ?? `ts_${Date.now().toString(36)}_${this.counter}`,
      tool,
      arguments: args,
    };
    if (this.sessionId) body.session_id = this.sessionId;
    const res = await fetch(`${this.baseUrl}/agent/v1/commands`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${this.token}` },
      body: JSON.stringify(body),
    });
    if (res.status >= 400) {
      throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
    }
    const env = (await res.json()) as Envelope;
    this.sessionId = env.session_id ?? this.sessionId;
    this.clockMs = env.clock_ms ?? this.clockMs;
    return env;
  }

  async ok<T = Record<string, unknown>>(tool: string, args: Record<string, unknown> = {}): Promise<T> {
    const env = await this.call(tool, args);
    if (env.status !== "ok") throw new CommandError(env);
    return env.data as T;
  }

  describe() {
    return this.ok("session.describe");
  }
  markets(args: Record<string, unknown> = {}) {
    return this.ok<{ items: Record<string, any>[]; next_cursor: number | null }>("markets.list", args);
  }
  async allMarkets(filters: Record<string, unknown> = {}): Promise<Record<string, any>[]> {
    const items: Record<string, any>[] = [];
    let cursor: number | null = null;
    for (;;) {
      const page = await this.markets({ limit: 200, ...(cursor === null ? {} : { cursor }), filters });
      items.push(...page.items);
      cursor = page.next_cursor;
      if (cursor === null) return items;
    }
  }
  market(poolId: string) {
    return this.ok("markets.get", { pool_id: poolId });
  }
  trades(poolId: string, args: Record<string, unknown> = {}) {
    return this.ok("market.trades", { pool_id: poolId, ...args });
  }
  candles(poolId: string, intervalMs: number, args: Record<string, unknown> = {}) {
    return this.ok("market.candles", { pool_id: poolId, interval_ms: intervalMs, ...args });
  }
  quote(poolId: string, assetIn: string, amountInRaw: bigint | string) {
    return this.ok<Record<string, any>>("broker.quote", { pool_id: poolId, asset_in: assetIn, amount_in_raw: amountInRaw.toString() });
  }
  submit(args: {
    poolId: string;
    assetIn: string;
    assetOut: string;
    amountInRaw: bigint | string;
    minAmountOutRaw: bigint | string;
    deadlineMs: number;
    idempotencyKey: string;
    quoteId?: string;
  }) {
    return this.call("broker.submit", {
      pool_id: args.poolId,
      asset_in: args.assetIn,
      asset_out: args.assetOut,
      amount_in_raw: args.amountInRaw.toString(),
      min_amount_out_raw: args.minAmountOutRaw.toString(),
      deadline_ms: args.deadlineMs,
      idempotency_key: args.idempotencyKey,
      ...(args.quoteId ? { quote_id: args.quoteId } : {}),
    });
  }
  order(orderId?: string) {
    return this.ok("broker.order", orderId ? { order_id: orderId } : {});
  }
  portfolio() {
    return this.ok<Record<string, any>>("portfolio.get");
  }
  history(cursor = 0, limit = 100) {
    return this.ok("portfolio.history", { cursor, limit });
  }
  advance(toMs: number) {
    return this.ok<{ clock_ms: number; episode_ended: boolean }>("clock.advance", { to_ms: toMs });
  }
  advanceNext(maxMs: number) {
    return this.ok<{ clock_ms: number; episode_ended: boolean }>("clock.advance", { next_event: true, max_ms: maxMs });
  }
  finish() {
    return this.ok<Record<string, any>>("session.finish");
  }
}

export function clientFromEnv(): MarketReplayClient {
  const url = process.env.MARKET_REPLAY_URL;
  const token = process.env.MARKET_REPLAY_TOKEN;
  if (!url || !token) {
    console.error("MARKET_REPLAY_URL and MARKET_REPLAY_TOKEN must be set");
    process.exit(2);
  }
  return new MarketReplayClient(url, token);
}
