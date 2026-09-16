import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { get, getToken, type Meta } from "./api";

/** Who the current browser is to the server: the operator (admin token) or anyone (public). */
export type RoleState = { role: "admin" | "public"; meta: Meta | null; loading: boolean; refresh: () => void };

const RoleContext = createContext<RoleState>({ role: "public", meta: null, loading: true, refresh: () => {} });

export function RoleProvider({ children, tick }: { children: ReactNode; tick: number }) {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [loading, setLoading] = useState(true);
  const [n, setN] = useState(0);
  const refresh = useCallback(() => setN((x) => x + 1), []);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    get<Meta>("/meta")
      .then((m) => alive && setMeta(m))
      .catch(() => alive && setMeta(null))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [n, tick]);
  const role = meta?.role === "admin" && getToken() ? "admin" : "public";
  return <RoleContext.Provider value={{ role, meta, loading, refresh }}>{children}</RoleContext.Provider>;
}

export function useRole(): RoleState {
  return useContext(RoleContext);
}
