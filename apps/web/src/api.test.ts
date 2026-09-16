// Seam: the control-plane client. Behaviour, not implementation: where requests go and how failures are classified.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, ISOLATIONS, api, getServerUrl, resolveApiBase, setServerUrl } from "./api";

const store = new Map<string, string>();
beforeEach(() => {
  store.clear();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  });
  setServerUrl("");
});
afterEach(() => vi.unstubAllGlobals());

describe("API base resolution", () => {
  it("defaults to the same origin (relative /api/v1)", () => {
    expect(resolveApiBase()).toBe("/api/v1");
  });
  it("uses a configured server URL and strips the trailing slash", () => {
    setServerUrl("http://127.0.0.1:8000/");
    expect(getServerUrl()).toBe("http://127.0.0.1:8000");
    expect(resolveApiBase()).toBe("http://127.0.0.1:8000/api/v1");
  });
  it("persists the server URL for the next page load", () => {
    setServerUrl("https://replay.example.org");
    expect(store.get("mr_server_url")).toBe("https://replay.example.org");
  });
});

describe("failure classification", () => {
  it("treats a 404 HTML response as 'no backend', with a message that says so", async () => {
    vi.stubGlobal("fetch", async () => new Response("<!doctype html><title>404</title>", { status: 404, headers: { "content-type": "text/html" } }));
    const err = await api("/health").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).kind).toBe("no_backend");
    expect((err as ApiError).message).toMatch(/no Market Replay server/i);
  });
  it("treats a 5xx HTML page as a server failure, not a missing backend", async () => {
    vi.stubGlobal("fetch", async () => new Response("<html>A server error has occurred</html>", { status: 500, headers: { "content-type": "text/html" } }));
    const err = (await api("/runs").catch((e) => e)) as ApiError;
    expect(err.kind).toBe("server");
    expect(err.message).toMatch(/server failed/i);
  });
  it("treats 401/403 as an auth problem", async () => {
    vi.stubGlobal("fetch", async () => new Response(JSON.stringify({ detail: { code: "UNAUTHORIZED" } }), { status: 401, headers: { "content-type": "application/json" } }));
    const err = (await api("/packs").catch((e) => e)) as ApiError;
    expect(err.kind).toBe("auth");
    expect(err.isAuth).toBe(true);
  });
  it("treats a JSON error from the server as an API error with its message", async () => {
    vi.stubGlobal("fetch", async () => new Response(JSON.stringify({ code: "NOT_RUNNABLE", message: "pack use status 'rejected' is not runnable" }), { status: 400, headers: { "content-type": "application/json" } }));
    const err = (await api("/runs", { method: "POST", body: "{}" }).catch((e) => e)) as ApiError;
    expect(err.kind).toBe("api");
    expect(err.message).toContain("not runnable");
  });
  it("treats a network failure as 'unreachable'", async () => {
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("Failed to fetch");
    });
    const err = (await api("/health").catch((e) => e)) as ApiError;
    expect(err.kind).toBe("unreachable");
  });
  it("sends requests to the configured server with the bearer token", async () => {
    const calls: [string, RequestInit][] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push([url, init]);
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    });
    setServerUrl("http://127.0.0.1:8000");
    store.set("mr_admin_token", "adm_x");
    await api("/packs");
    expect(calls[0]?.[0]).toBe("http://127.0.0.1:8000/api/v1/packs");
    expect((calls[0]?.[1].headers as Record<string, string>).Authorization).toBe("Bearer adm_x");
  });
});

describe("run form contract", () => {
  it("offers exactly the isolation values the server accepts", () => {
    expect([...ISOLATIONS]).toEqual(["trusted_external_client", "restricted_local_runner"]);
  });
});
