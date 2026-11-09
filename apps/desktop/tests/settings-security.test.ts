import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { isTrustedRenderer } from "../src/main/ipc-security";
import { SecureRefreshTokenStore } from "../src/main/secure-refresh-store";
import { SettingsStore } from "../src/main/settings-store";
import { applyWindowSettings, closeToTray, setExpanded } from "../src/main/window-controller";

const tempFolders: string[] = [];
function tempFolder(): string {
  const path = mkdtempSync(join(tmpdir(), "interview-desktop-test-"));
  tempFolders.push(path);
  return path;
}

afterEach(() => {
  for (const path of tempFolders.splice(0)) rmSync(path, { recursive: true, force: true });
});

describe("secure refresh-token storage", () => {
  it("encrypts refresh credentials before writing them and restores only through safeStorage", () => {
    const folder = tempFolder();
    const path = join(folder, "refresh.bin");
    const safeStorage = {
      isEncryptionAvailable: () => true,
      encryptString: (value: string) => Buffer.from(`encrypted:${Buffer.from(value).toString("base64")}`),
      decryptString: (value: Buffer) =>
        Buffer.from(value.toString().slice("encrypted:".length), "base64").toString(),
    };
    const store = new SecureRefreshTokenStore(path, safeStorage);
    store.save("sensitive-refresh-token");

    expect(readFileSync(path, "utf8")).not.toContain("sensitive-refresh-token");
    expect(store.load()).toBe("sensitive-refresh-token");
    store.clear();
    expect(store.load()).toBeNull();
  });

  it("refuses persistence if operating-system encryption is unavailable", () => {
    const store = new SecureRefreshTokenStore(join(tempFolder(), "refresh.bin"), {
      isEncryptionAvailable: () => false,
      encryptString: () => Buffer.alloc(0),
      decryptString: () => "",
    });
    expect(() => store.save("token")).toThrow("Windows secure storage is unavailable");
  });
});

describe("desktop settings", () => {
  it("persists preferences and rejects insecure backend URLs and invisible opacity", () => {
    const filePath = join(tempFolder(), "settings.json");
    const store = new SettingsStore(filePath);
    store.load();
    const updated = store.update({ backendBaseUrl: "https://api.example.com", opacity: 0.72 });
    expect(updated.backendBaseUrl).toBe("https://api.example.com");
    expect(JSON.parse(readFileSync(filePath, "utf8"))).toMatchObject({
      backendBaseUrl: "https://api.example.com",
      opacity: 0.72,
    });
    expect(() => store.update({ backendBaseUrl: "http://example.com" })).toThrow();
    expect(() => store.update({ opacity: 0.1 })).toThrow();
  });
});

describe("IPC renderer allowlist", () => {
  const htmlPath = "C:\\assistant\\dist\\renderer\\index.html";

  it("accepts only the configured development origin and renderer page", () => {
    expect(
      isTrustedRenderer("http://localhost:5173/", "http://localhost:5173/", htmlPath),
    ).toBe(true);
    expect(
      isTrustedRenderer("http://localhost:5173.evil.test/", "http://localhost:5173/", htmlPath),
    ).toBe(false);
    expect(
      isTrustedRenderer("https://evil.test/", "http://localhost:5173/", htmlPath),
    ).toBe(false);
  });

  it("accepts only the packaged renderer file", () => {
    expect(isTrustedRenderer("file:///C:/assistant/dist/renderer/index.html", undefined, htmlPath)).toBe(true);
    expect(isTrustedRenderer("file:///C:/assistant/other.html", undefined, htmlPath)).toBe(false);
    expect(isTrustedRenderer("https://example.com", undefined, htmlPath)).toBe(false);
  });
});

describe("window behavior", () => {
  it("applies sizes, opacity bounds and always-on-top preference", () => {
    const calls: unknown[][] = [];
    const window = {
      getBounds: () => ({ x: 0, y: 0, width: 390, height: 520 }),
      setSize: (...args: [number, number, boolean?]) => calls.push(["size", ...args]),
      setMinimumSize: (...args: [number, number]) => calls.push(["minimum", ...args]),
      setOpacity: (value: number) => calls.push(["opacity", value]),
      setAlwaysOnTop: (value: boolean) => calls.push(["top", value]),
    };
    setExpanded(window, true);
    applyWindowSettings(window, { opacity: 0.1, alwaysOnTop: false });
    expect(calls).toContainEqual(["size", 760, 740, true]);
    expect(calls).toContainEqual(["opacity", 0.65]);
    expect(calls).toContainEqual(["top", false]);
  });

  it("hides to the visible tray on close but permits an explicit application quit", () => {
    let prevented = false;
    let hidden = 0;
    const event = { preventDefault: () => { prevented = true; } };
    const window = { hide: () => { hidden += 1; } };
    closeToTray(event, window, false);
    expect(prevented).toBe(true);
    expect(hidden).toBe(1);
    closeToTray(event, window, true);
    expect(hidden).toBe(1);
  });
});
