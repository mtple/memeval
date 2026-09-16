// Seam: the request body the New run screen sends. The server contract is fixed (RunBody in service/app.py).
import { describe, expect, it } from "vitest";
import { buildCreateRunBody, buildNewRunBody, validateCreateRun, validateNewRun, type NewRunForm } from "./forms";

const base = { agent_id: "agent_1", pack_id: "pack_1", mode: "practice", bankroll_raw: "1000000", isolation: "trusted_external_client", launchKind: "example", example: "cash_only", runtime: "python", agent_seed: "" };

describe("buildCreateRunBody", () => {
  it("keeps agent_seed a string (the server never receives a JS number)", () => {
    const body = buildCreateRunBody({ ...base, agent_seed: "7" });
    expect(body.agent_seed).toBe("7");
  });
  it("omits agent_seed when blank", () => {
    expect("agent_seed" in buildCreateRunBody(base)).toBe(false);
  });
  it("adds a launch only for included reference agents", () => {
    expect(buildCreateRunBody(base).launch).toEqual({ kind: "example", name: "cash_only", runtime: "python" });
    expect("launch" in buildCreateRunBody({ ...base, launchKind: "external" })).toBe(false);
  });
  it("passes isolation through unchanged", () => {
    expect(buildCreateRunBody({ ...base, isolation: "restricted_local_runner" }).isolation).toBe("restricted_local_runner");
  });
});

describe("validateCreateRun", () => {
  it("rejects a non-integer bankroll and an unknown isolation", () => {
    expect(validateCreateRun({ ...base, bankroll_raw: "1.5" }).join(" ")).toContain("bankroll");
    expect(validateCreateRun({ ...base, isolation: "none" }).join(" ")).toContain("isolation");
    expect(validateCreateRun(base)).toEqual([]);
  });
});

const fresh: NewRunForm = { driver: "own", agent_name: "bankr-bot", agent_version: "1", pack_id: "pack_1", example: "cash_only", mode: "practice", bankroll_raw: "1000000", agent_seed: "" };

describe("buildNewRunBody (self-serve)", () => {
  it("sends the agent inline by name and version instead of an agent_id", () => {
    const body = buildNewRunBody(fresh);
    expect(body.agent).toEqual({ name: "bankr-bot", version: "1", runtime: "external" });
    expect("agent_id" in body).toBe(false);
    expect("launch" in body).toBe(false);
  });
  it("launches a reference participant when the server drives the run", () => {
    const body = buildNewRunBody({ ...fresh, driver: "reference", example: "scheduled_basket" });
    expect(body.launch).toEqual({ kind: "example", name: "scheduled_basket", runtime: "python" });
    expect(body.agent).toEqual({ name: "scheduled_basket_python", version: "1", runtime: "python" });
  });
  it("trims the name and defaults a blank version to 1", () => {
    const body = buildNewRunBody({ ...fresh, agent_name: "  bot ", agent_version: " " });
    expect(body.agent).toEqual({ name: "bot", version: "1", runtime: "external" });
  });
});

describe("validateNewRun", () => {
  it("needs a name for your own agent, an episode and a whole-number bankroll", () => {
    expect(validateNewRun({ ...fresh, agent_name: "" }).join(" ")).toContain("name");
    expect(validateNewRun({ ...fresh, pack_id: "" }).join(" ")).toContain("episode");
    expect(validateNewRun({ ...fresh, bankroll_raw: "x" }).join(" ")).toContain("bankroll");
    expect(validateNewRun(fresh)).toEqual([]);
    expect(validateNewRun({ ...fresh, driver: "reference", agent_name: "" })).toEqual([]);
  });
});
