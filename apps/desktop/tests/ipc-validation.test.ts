import { describe, expect, it } from "vitest";
import { isDisplayId, isUuid, validateCopyText, validateSettingsPatch } from "../src/main/ipc-validation";

describe("settings patch validation", () => {
  it("accepts in-range opacity and known keys", () => {
    expect(validateSettingsPatch({ opacity: 0.65, alwaysOnTop: false })).toEqual({
      opacity: 0.65,
      alwaysOnTop: false,
    });
    expect(validateSettingsPatch({ opacity: 1 })).toEqual({ opacity: 1 });
  });

  it("gives a specific message for out-of-range opacity", () => {
    for (const opacity of [0.5, 0, -1, 1.01, 5]) {
      expect(() => validateSettingsPatch({ opacity })).toThrow("Opacity must be between 65% and 100%.");
    }
  });

  it("rejects non-numeric and non-finite opacity as not a number", () => {
    for (const opacity of ["0.9", null, Number.NaN, Infinity]) {
      expect(() => validateSettingsPatch({ opacity })).toThrow("Opacity must be a number.");
    }
  });

  it("still rejects unknown keys and wrong types", () => {
    expect(() => validateSettingsPatch({ evil: 1 })).toThrow("Unsupported setting.");
    expect(() => validateSettingsPatch({ alwaysOnTop: "yes" })).toThrow("on or off");
    expect(() => validateSettingsPatch([])).toThrow("Settings must be an object.");
  });
});

describe("copy text validation", () => {
  it("passes strings through", () => {
    expect(validateCopyText("hello")).toBe("hello");
    expect(validateCopyText("")).toBe("");
  });
  it("distinguishes non-string from too large", () => {
    expect(() => validateCopyText(42)).toThrow("Only text can be copied.");
    expect(() => validateCopyText({})).toThrow("Only text can be copied.");
    expect(() => validateCopyText("a".repeat(100_001))).toThrow("The text is too large to copy.");
    expect(validateCopyText("a".repeat(100_000))).toHaveLength(100_000);
  });
});

describe("screenshot-related validation (Phase 11B)", () => {
  it("accepts a boolean screenshot setting and rejects anything else", () => {
    expect(validateSettingsPatch({ screenshotInputEnabled: true })).toEqual({ screenshotInputEnabled: true });
    for (const value of ["true", 1, null, {}]) {
      expect(() => validateSettingsPatch({ screenshotInputEnabled: value })).toThrow("Screenshot input must be on or off.");
    }
  });

  it("validates ids used by the capture channels", () => {
    expect(isUuid("11111111-2222-4333-8444-555555555555")).toBe(true);
    for (const value of ["x", "", 5, null, "11111111-2222-4333-8444-55555555555g", "../../etc/passwd"]) {
      expect(isUuid(value)).toBe(false);
    }
    expect(isDisplayId("2528732444")).toBe(true);
    for (const value of ["", "abc", "12 34", "1".repeat(13), 12, "-1", "1;2"]) {
      expect(isDisplayId(value)).toBe(false);
    }
  });
});
