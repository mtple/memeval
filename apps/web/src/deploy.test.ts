// Seam: static-host deployment config. A static host must serve index.html for client routes but never swallow API paths.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

type Rewrite = { source: string; destination: string };

function loadRewrites(path: string): Rewrite[] {
  return (JSON.parse(readFileSync(path, "utf8")) as { rewrites: Rewrite[] }).rewrites;
}

function matches(source: string, path: string): boolean {
  // Vercel sources are path-to-regexp; the one we use is a single regex capture group.
  const inner = source.replace(/^\/\(/, "").replace(/\)$/, "");
  return new RegExp(`^/${inner}$`).test(path);
}

describe.each([["apps/web/vercel.json", new URL("../vercel.json", import.meta.url).pathname], ["vercel.json (repo root)", new URL("../../../vercel.json", import.meta.url).pathname]])("%s", (_label, file) => {
  const rewrites = loadRewrites(file);
  it("rewrites client routes to index.html", () => {
    for (const p of ["/agents", "/episodes", "/runs/run_abc", "/runs/run_abc/results", "/compare/cmp_1", "/data-health/pack_x"]) {
      expect(rewrites.some((r) => matches(r.source, p) && r.destination === "/index.html"), p).toBe(true);
    }
  });
  it("never rewrites API, agent-plane or asset paths", () => {
    for (const p of ["/api/v1/health", "/agent/v1/commands", "/assets/index-abc.js"]) {
      expect(rewrites.some((r) => matches(r.source, p)), p).toBe(false);
    }
  });
});
