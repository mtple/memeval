// Seam: the request body the Run screen sends. The server contract is fixed (RunBody in service/app.py).
import { describe, expect, it } from "vitest";
import { buildCreateRunBody, validateCreateRun } from "./forms";

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
