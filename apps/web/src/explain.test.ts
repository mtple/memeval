// Seam: the words a person reads on a result. Every status value the report can carry gets a sentence, never a raw token.
import { describe, expect, it } from "vitest";
import { explainDimension, summarySentence, trustTags } from "./explain";

describe("explainDimension", () => {
  it("turns each status value into a sentence a non-expert can read", () => {
    expect(explainDimension("data_origin", "generated_fixture")).toMatch(/artificial market/i);
    expect(explainDimension("availability_basis", "fixture_delay_model")).toMatch(/delay/i);
    expect(explainDimension("execution_model", "cpmm_fixed_flow_v1")).toMatch(/constant-product/i);
    expect(explainDimension("isolation", "trusted_external_client")).toMatch(/outside this server/i);
    expect(explainDimension("isolation", "in_process_reference_participant")).toMatch(/inside the server/i);
    expect(explainDimension("use_status", "demo")).toMatch(/demonstration/i);
    expect(explainDimension("predictive_validity", "not_established")).toMatch(/does not predict/i);
  });
  it("never returns a raw underscore token, even for unknown values", () => {
    const s = explainDimension("token_behavior", "some_new_basis_v9");
    expect(s).not.toMatch(/_/);
    expect(s.length).toBeGreaterThan(10);
  });
});

describe("summarySentence", () => {
  const base = { agent: "bankr-bot", episode: "gen_week_trending", durationMs: 7 * 86_400_000, isFullWeek: true, unit: "CASH", decimals: 6, initialRaw: "1000000", terminalRaw: "1100000", headlineReturn: "0.1", valuationComplete: true, orders: 5, fills: 4, gasRaw: "2000" };
  it("states start, end, return and activity in one paragraph", () => {
    const s = summarySentence(base);
    expect(s).toContain("bankr-bot");
    expect(s).toContain("started with 1 CASH");
    expect(s).toContain("ended with 1.1 CASH");
    expect(s).toContain("+10.00%");
    expect(s).toContain("4 of 5 orders filled");
  });
  it("says plainly when the agent never traded", () => {
    expect(summarySentence({ ...base, orders: 0, fills: 0, terminalRaw: "1000000", headlineReturn: "0" })).toMatch(/never placed an order/);
  });
  it("refuses to state a return when the valuation is incomplete", () => {
    const s = summarySentence({ ...base, valuationComplete: false, headlineReturn: null, terminalRaw: null, unpriced: 2 });
    expect(s).toMatch(/could not be valued/);
    expect(s).not.toMatch(/%/);
  });
});

describe("trustTags", () => {
  it("gives three short readable tags for a result card", () => {
    const tags = trustTags({ data_origin: "generated_fixture", availability_basis: "fixture_delay_model", isolation: "trusted_external_client" }, true);
    expect(tags).toEqual(["artificial market", "delayed prices", "fully valued"]);
    expect(trustTags({ data_origin: "onchain_recorded" }, false)[2]).toBe("not fully valued");
  });
});
