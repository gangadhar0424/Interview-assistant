/** D-65: the desktop has no sign-in or sign-up. Accounts live on the web; the desktop receives its
 * login only from the Start Interview handoff (D-56). Source guards so this cannot quietly return. */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const read = (relative: string) => readFileSync(join(__dirname, "..", relative), "utf8");

describe("the desktop has no password sign-in or account creation (D-65)", () => {
  it("the signed-out screen has no form, password field or sign-in/create-account controls", () => {
    const app = read("src/renderer/src/ui/App.tsx");
    const start = app.indexOf("function SignedOutScreen()");
    expect(start).toBeGreaterThan(-1);
    const screen = app.slice(start, app.indexOf("\n}\n", start));
    expect(screen).not.toMatch(/<form|type="password"|type="email"|<input/);
    expect(screen).not.toMatch(/Create account|>Sign in</);
    expect(screen).toContain("Start Interview");
    expect(app).not.toContain("<LoginScreen");
  });

  it("no login or signup is exposed to the renderer or handled over IPC", () => {
    const preload = read("src/preload/index.ts");
    const ipc = read("src/main/ipc.ts");
    const contracts = read("src/shared/contracts.ts");
    const store = read("src/renderer/src/store.ts");
    expect(preload).not.toMatch(/auth:login|auth:signup/);
    expect(ipc).not.toMatch(/auth:login|auth:signup/);
    expect(contracts).not.toMatch(/\blogin\(|\bsignup\(/);
    expect(store).not.toMatch(/\blogin\b|\bsignup\b/);
  });

  it("the API client can no longer create an account", () => {
    expect(read("src/main/api-client.ts")).not.toContain("/api/v1/auth/signup");
  });
});
