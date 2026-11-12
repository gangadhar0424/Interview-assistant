/**
 * Web -> desktop handoff (D-55): strict deep-link parsing, argv extraction (cold start and a second
 * instance) and the early-link inbox. Pure functions, no Electron.
 */
import { describe, expect, it, vi } from "vitest";
import { DeepLinkInbox, extractLinkFromArgv, parseHandoffLink, PROTOCOL_SCHEME } from "../src/main/deep-link";

const CODE = "Ab3-_".repeat(8) + "xyz"; // 43 url-safe characters
const LINK = `interviewassistant://start?h=${CODE}`;

describe("parseHandoffLink", () => {
  it("accepts exactly the documented link and returns only the code", () => {
    expect(CODE).toHaveLength(43);
    expect(parseHandoffLink(LINK)).toBe(CODE);
    expect(parseHandoffLink(`${LINK.replace("interviewassistant", "InterviewAssistant")}`)).toBe(CODE);
    expect(parseHandoffLink(`interviewassistant://start/?h=${CODE}`)).toBe(CODE); // trailing slash only
    expect(PROTOCOL_SCHEME).toBe("interviewassistant");
  });

  it.each([
    ["wrong scheme", `https://start?h=${CODE}`],
    ["another custom scheme", `other://start?h=${CODE}`],
    ["scheme only", "interviewassistant://"],
    ["no code", "interviewassistant://start"],
    ["empty code", "interviewassistant://start?h="],
    ["wrong host", `interviewassistant://open?h=${CODE}`],
    ["a path", `interviewassistant://start/anything?h=${CODE}`],
    ["a fragment", `${LINK}#x`],
    ["an extra parameter", `${LINK}&session=abc`],
    ["a parameter before the code", `interviewassistant://start?x=1&h=${CODE}`],
    ["a duplicated code", `${LINK}&h=${CODE}`],
    ["userinfo", `interviewassistant://user:pw@start?h=${CODE}`],
    ["a port", `interviewassistant://start:80?h=${CODE}`],
    ["a code that is too short", `interviewassistant://start?h=${CODE.slice(1)}`],
    ["a code that is too long", `${LINK}a`],
    ["a code with a bad character", `interviewassistant://start?h=${CODE.slice(0, 42)}!`],
    ["a percent-encoded code", `interviewassistant://start?h=${CODE.slice(0, 40)}%41%41%41`],
    ["path traversal", "interviewassistant://start?h=../../../../etc/passwd"],
    ["whitespace", `${LINK} `],
    ["a newline", `${LINK}\n`],
    ["a NUL byte", `${LINK}\u0000`],
    ["a backslash", `interviewassistant:\\\\start?h=${CODE}`],
    ["an oversized value", `interviewassistant://start?h=${"A".repeat(5000)}`],
    ["javascript", "javascript:alert(1)"],
    ["an empty string", ""],
  ])("rejects %s", (_name, raw) => {
    expect(parseHandoffLink(raw)).toBeNull();
  });

  it.each([undefined, null, 42, {}, [], true])("rejects non-strings (%s)", (value) => {
    expect(parseHandoffLink(value)).toBeNull();
  });

  it("never throws, whatever it is given", () => {
    for (const raw of ["%", "interviewassistant://start?h=%E0%A4%A", "\uD800", "interviewassistant://start?h=\uD800"]) {
      expect(() => parseHandoffLink(raw)).not.toThrow();
      expect(parseHandoffLink(raw)).toBeNull();
    }
  });
});

describe("extractLinkFromArgv (cold start and second instance)", () => {
  it("finds the link among the arguments the OS passes on Windows", () => {
    expect(extractLinkFromArgv(["C:\\App\\Interview Assistant.exe", LINK])).toBe(LINK);
    expect(extractLinkFromArgv(["app.exe", "--flag", LINK, "--other"])).toBe(LINK);
  });

  it("ignores everything else", () => {
    expect(extractLinkFromArgv([])).toBeNull();
    expect(extractLinkFromArgv(["app.exe", "--inspect", "https://example.com"])).toBeNull();
    expect(extractLinkFromArgv(["app.exe", 7, null, undefined])).toBeNull();
  });

  it("returns the raw argument; strict validation still happens in parseHandoffLink", () => {
    const bad = "interviewassistant://start?h=short";
    expect(extractLinkFromArgv(["app.exe", bad])).toBe(bad);
    expect(parseHandoffLink(bad)).toBeNull();
  });
});

describe("DeepLinkInbox", () => {
  it("holds a link that arrives before the controller exists and delivers it on attach", () => {
    const inbox = new DeepLinkInbox();
    inbox.deliver(LINK);
    const handler = vi.fn();
    inbox.attach(handler);
    expect(handler).toHaveBeenCalledExactlyOnceWith(LINK);
  });

  it("delivers straight away once attached (the already-running case)", () => {
    const inbox = new DeepLinkInbox();
    const handler = vi.fn();
    inbox.attach(handler);
    inbox.deliver(LINK);
    expect(handler).toHaveBeenCalledExactlyOnceWith(LINK);
  });

  it("keeps only the newest few early links so a flood cannot grow memory", () => {
    const inbox = new DeepLinkInbox();
    for (let i = 0; i < 50; i += 1) inbox.deliver(`l${i}`);
    const handler = vi.fn();
    inbox.attach(handler);
    expect(handler.mock.calls.map((c) => c[0])).toEqual(["l47", "l48", "l49"]);
  });

  it("forgets held links and stops delivering after detach", () => {
    const inbox = new DeepLinkInbox();
    inbox.deliver(LINK);
    inbox.detach();
    const handler = vi.fn();
    inbox.attach(handler);
    expect(handler).not.toHaveBeenCalled();
    inbox.detach();
    inbox.deliver(LINK);
    expect(handler).not.toHaveBeenCalled(); // held again, not delivered to the detached handler
  });
});
