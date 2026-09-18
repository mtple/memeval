import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CandleChart } from "./charts";
import { TradeReview } from "./TradeReview";
import type { Bar } from "./api";

const bar: Bar = { start_ms: 0, end_ms: 60000, open: "1", high: "2", low: "1", close: "2", volume_base_raw: "1", volume_quote_raw: "1", trade_count: 2, closed: true, synthetic_empty_bar: false, completeness: "complete" };
describe("trade review", () => {
  it("is opt-in, without triggering historical run reconstruction on page load", () => {
    const html = renderToStaticMarkup(<TradeReview runId="run_test" />);
    expect(html).toContain("Load trade review");
    expect(html).not.toContain("price chart");
  });
  it("renders buy/sell markers with fill price and status and hides future fills", () => {
    const html = renderToStaticMarkup(<CandleChart bars={[bar]} gaps={[]} clockMs={60000} markers={[
      { time_ms: 10000, price: "0.8", label: "B · confirmed", side: "buy" },
      { time_ms: 40000, price: "2.2", label: "S · confirmed", side: "sell" },
      { time_ms: 70000, price: "9", label: "future-fill", side: "buy" },
    ]} />);
    expect(html).toContain("B · confirmed");
    expect(html).toContain("S · confirmed");
    expect(html).toContain("price 0.8");
    expect(html).not.toContain("future-fill");
    expect(html).not.toContain("NaN");
  });
});
