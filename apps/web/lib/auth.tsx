"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { apiRequest, apiStream, ApiError, type RequestOptions, type StreamOptions } from "./api";

export type User = { id: string; email: string; created_at: string };
type TokenResponse = { access_token: string; expires_in: number; user: User };
type Session = { accessToken: string; user: User };

// Credential handling:
//  - The long-lived refresh token lives ONLY in an HttpOnly cookie set by the API; JavaScript
//    never sees it.
//  - The short-lived access token is kept in memory only (never localStorage/sessionStorage),
//    so it is gone on reload and recovered via the refresh cookie.
function toSession(r: TokenResponse): Session {
  return { accessToken: r.access_token, user: r.user };
}

// Refresh tokens rotate and reuse is treated as theft, so concurrent refreshes (e.g. React
// StrictMode double-invoking effects) must share ONE in-flight request.
let inflightRefresh: Promise<Session | null> | null = null;

export function refreshSession(): Promise<Session | null> {
  if (!inflightRefresh) {
    inflightRefresh = apiRequest<TokenResponse>("/api/v1/auth/refresh", { method: "POST", body: {} })
      .then(toSession)
      .catch((err: unknown) => {
        if (err instanceof ApiError) return null; // 401 etc. => not signed in
        return null;
      })
      .finally(() => {
        inflightRefresh = null;
      });
  }
  return inflightRefresh;
}

type AuthState =
  | { status: "loading"; session: null }
  | { status: "anonymous"; session: null }
  | { status: "authenticated"; session: Session };

type AuthContextValue = {
  state: AuthState;
  signup: (email: string, password: string) => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Run an authenticated call; on a 401 refresh once and retry. */
  authedRequest: <T>(path: string, options?: RequestOptions) => Promise<T>;
  /** Same as authedRequest (refresh once on a 401) for an SSE POST: returns the live Response. */
  authedStream: (path: string, options: Omit<StreamOptions, "token">) => Promise<Response>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading", session: null });

  useEffect(() => {
    let cancelled = false;
    void refreshSession().then((session) => {
      if (cancelled) return;
      setState(session ? { status: "authenticated", session } : { status: "anonymous", session: null });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const r = await apiRequest<TokenResponse>("/api/v1/auth/login", { body: { email, password } });
    setState({ status: "authenticated", session: toSession(r) });
  }, []);

  const signup = useCallback(
    async (email: string, password: string) => {
      await apiRequest("/api/v1/auth/signup", { body: { email, password } });
      await login(email, password);
    },
    [login],
  );

  const logout = useCallback(async () => {
    try {
      await apiRequest("/api/v1/auth/logout", { method: "POST", body: {} });
    } finally {
      setState({ status: "anonymous", session: null }); // always drop local state
    }
  }, []);

  const authedRequest = useCallback(
    async <T,>(path: string, options: RequestOptions = {}): Promise<T> => {
      if (state.status !== "authenticated") throw new ApiError(401, "not_authenticated", "Not signed in.");
      try {
        return await apiRequest<T>(path, { ...options, token: state.session.accessToken });
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 401) throw err;
        const renewed = await refreshSession();
        if (!renewed) {
          setState({ status: "anonymous", session: null });
          throw err;
        }
        setState({ status: "authenticated", session: renewed });
        return apiRequest<T>(path, { ...options, token: renewed.accessToken });
      }
    },
    [state],
  );

  const authedStream = useCallback(
    async (path: string, options: Omit<StreamOptions, "token">): Promise<Response> => {
      if (state.status !== "authenticated") throw new ApiError(401, "not_authenticated", "Not signed in.");
      try {
        return await apiStream(path, { ...options, token: state.session.accessToken });
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 401) throw err;
        const renewed = await refreshSession();
        if (!renewed) {
          setState({ status: "anonymous", session: null });
          throw err;
        }
        setState({ status: "authenticated", session: renewed });
        return apiStream(path, { ...options, token: renewed.accessToken });
      }
    },
    [state],
  );

  const value = useMemo(
    () => ({ state, signup, login, logout, authedRequest, authedStream }),
    [state, signup, login, logout, authedRequest, authedStream],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
