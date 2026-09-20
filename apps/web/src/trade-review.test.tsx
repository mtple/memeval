// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { TradeBreakdown, TradeReview } from "./TradeReview";
import { TradePriceChart, tradeChartData, type TradeMarker } from "./TradePriceChart";
import type { Bar } from "./api";

Object.assign(globalThis, {IS_REACT_ACT_ENVIRONMENT: true});

const bar = (start: number, close = "0.000000015"): Bar => ({start_ms:start,end_ms:start+60_000,open:close,high:close,low:close,close,volume_base_raw:"1",volume_quote_raw:"1",trade_count:2,closed:true,synthetic_empty_bar:false,completeness:"complete"});
const markers: TradeMarker[] = [
  {id:"buy",time_ms:60_000,price:"0.000000015",side:"buy",label:"Buy 1",detail:"Paid 0.003 ETH · Filled and confirmed"},
  {id:"sell",time_ms:120_000,price:"0.0000000165",side:"sell",label:"Sell 1",detail:"Received 0.0033 ETH · Filled and confirmed"},
];

describe("readable trade results", () => {
  it("loads the trade review automatically", () => {
    const html = renderToStaticMarkup(<TradeReview runId="run_test"/>);
    expect(html).toContain("Which trades changed the balance?");
    expect(html).not.toContain("Load trade review");
  });
  it("uses first buy as zero and preserves gaps instead of inventing a continuous price", () => {
    const data = tradeChartData([bar(0),bar(60_000,"0.0000000165"),bar(240_000),bar(900_000)], [{start_ms:120_000,end_ms:240_000,reason:"missing"}], [...markers,{...markers[0]!,id:"future",time_ms:900_000}],300_000,true);
    expect(data.reference).toBe(0.000000015);
    expect(data.points[1]!.value).toBeCloseTo(10);
    expect(data.segments).toHaveLength(2);
    expect(data.fills).toHaveLength(2);
    expect(data.points).toHaveLength(3);
    expect(data.gaps[0]!.start_ms).toBe(120_000);
  });
  it("keeps observation times truthful at the window and virtual-clock boundaries", () => {
    const nearEnd = {...markers[0]!, time_ms:60_500};
    const data = tradeChartData([bar(900_000),bar(960_000)], [], [nearEnd], 2_000_000, false);
    expect(data.end).toBe(960_500);
    expect(data.points.map(p => p.time)).toEqual([960_000]);
    const clockBound = tradeChartData([bar(60_000),{...bar(60_000),closed:false}], [], markers, 90_000, true);
    expect(clockBound.points.map(p => p.time)).toEqual([90_000]);
    const stored = {...bar(60_000), closed:undefined} as unknown as Bar;
    expect(tradeChartData([stored], [], markers, 90_000, true).points).toHaveLength(0);
  });
  it("shows usable labels for tiny or flat prices and never calls the chart account return", () => {
    const html = renderToStaticMarkup(<TradePriceChart bars={[bar(0),bar(60_000)]} gaps={[]} markers={markers} clockMs={300_000} token="Token 1"/>);
    expect(html).toContain("Buy 1");
    expect(html).toContain("Sell 1");
    expect(html).toContain("first buy price");
    expect(html).toContain("not your account return");
    expect(html).toContain("Time elapsed since replay start");
    expect(html).not.toMatch(/NaN|Infinity|nano-ETH/);
  });
  it("supports keyboard selection and changing the chart time range", async () => {
    const container = document.createElement("div"); document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<TradePriceChart bars={[bar(0),bar(60_000)]} gaps={[]} markers={markers} clockMs={3_600_000} token="Token 1"/>));
    const sell = container.querySelector<SVGGElement>('[role="button"][aria-label^="Sell 1"]')!;
    await act(async () => sell.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true})));
    expect(container.querySelector(".trade-chart-readout")!.textContent).toContain("Received 0.0033 ETH");
    const full = [...container.querySelectorAll("button")].find(b => b.textContent === "Full replay")!;
    await act(async () => full.click());
    expect(full.getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelector("svg")!.textContent).toContain("01:00");
    await act(async () => root.unmount()); container.remove();
  });
  it("explains net cash with accurate small gas amounts and handles empty histories", () => {
    const token = {asset_id:"asset_a",decimals:18,eth_spent_raw:"3000000000000000",eth_recovered_raw:"3100000000000000",gas_raw:"1500000000000",net_cash_raw:"98500000000000",remaining_raw:"0",pending_raw:"0",buys:1,sells:1,first_buy_ms:60_000,last_sell_ms:120_000,average_hold_ms:60_000};
    const event = {order_id:"order_a",pool_id:"pool_a",asset_id:"asset_a",side:"buy",state:"confirmed",submitted_ms:60_000,fill_time_ms:60_000,confirm_time_ms:62_000,quantity_raw:"1",cash_raw:token.eth_spent_raw,gas_raw:token.gas_raw,price:"0.000000015",reason:null};
    const html = renderToStaticMarkup(<TradeBreakdown runId="test" r={{clock_ms:300_000,numeraire:"NATIVE",numeraire_decimals:18,tokens:[token],events:[event],rejected_calls:[],note:"",series:{pool_a:{pool_id:"pool_a",interval_ms:60_000,items:[],gaps:[],as_of_ms:300_000}}}}/>);
    expect(html).toContain("+0.0000985 ETH");
    expect(html).toContain("0.0000015 ETH");
    expect(html).toContain("Pool fees are already reflected");
    expect(html).toContain("No observed market history");
    expect(html).not.toContain("never sold");
  });
});
