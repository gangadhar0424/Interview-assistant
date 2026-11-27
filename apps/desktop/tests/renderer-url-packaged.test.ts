import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { rendererDevUrl } from "../src/main/ipc-security";

// E-01 regression: a packaged build must ignore ELECTRON_RENDERER_URL.
describe("rendererDevUrl", () => {
  const env = { ELECTRON_RENDERER_URL: "http://evil.example:5173/" };

  it("is ignored in a packaged build", () => {
    expect(rendererDevUrl(env, true)).toBeUndefined();
  });
  it("is honoured in development", () => {
    expect(rendererDevUrl(env, false)).toBe("http://evil.example:5173/");
  });
  it("is undefined when unset or empty", () => {
    expect(rendererDevUrl({}, false)).toBeUndefined();
    expect(rendererDevUrl({ ELECTRON_RENDERER_URL: "" }, false)).toBeUndefined();
  });
  it("main reads the variable only through the helper", () => {
    const source = readFileSync(join(__dirname, "../src/main/index.ts"), "utf8");
    expect(source.match(/process\.env\.ELECTRON_RENDERER_URL/g) ?? []).toHaveLength(0);
    expect(source).toContain("rendererDevUrl(process.env, app.isPackaged)");
  });
});
