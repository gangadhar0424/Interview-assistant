// Static security headers for every web response (Phase 13A).
//
// Deliberately NOT included: a `script-src`/`style-src` Content-Security-Policy. Next.js needs a
// per-request nonce (a proxy plus dynamic rendering of every page) for a strict script policy; that
// is an architecture change and is tracked as a documented limitation (docs/SECURITY_REVIEW.md).
// The directives below do not affect script or style loading, so they cannot break the app.
// HSTS is set by the TLS-terminating proxy (Phase 14A), not here.

export interface HeaderEntry {
  key: string;
  value: string;
}

export const CSP_NON_SCRIPT_DIRECTIVES = [
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

export const SECURITY_HEADERS: readonly HeaderEntry[] = [
  { key: "Content-Security-Policy", value: CSP_NON_SCRIPT_DIRECTIVES },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), display-capture=(), payment=()",
  },
];
