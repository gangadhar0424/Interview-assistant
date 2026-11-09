import { describe, expect, it } from "vitest";
import { parseSseEvent, SseParser, SseProtocolError } from "../src/main/sse";

describe("SSE framing", () => {
  it("preserves CRLF delimiters split across network chunks", () => {
    const parser = new SseParser();
    expect(parser.push('event: answer.delta\r\ndata: {"answer_id":"a1","index":0,"text":"Hi"}\r')).toEqual(
      [],
    );
    expect(parser.push("\n\r")).toEqual([]);
    const frames = parser.push("\n");
    expect(frames).toEqual([
      {
        event: "answer.delta",
        data: '{"answer_id":"a1","index":0,"text":"Hi"}',
      },
    ]);
    expect(parseSseEvent(frames[0]!)).toEqual({
      type: "answer.delta",
      answerId: "a1",
      index: 0,
      text: "Hi",
    });
  });

  it("joins multiple data lines as specified by SSE", () => {
    const parser = new SseParser();
    expect(parser.push('event: answer.delta\ndata: {"answer_id":\ndata: "a1"}\n\n')).toEqual([
      { event: "answer.delta", data: '{"answer_id":\n"a1"}' },
    ]);
  });

  it("rejects malformed JSON and oversized buffered frames", () => {
    expect(() => parseSseEvent({ event: "answer.delta", data: "{" })).toThrow(SseProtocolError);
    expect(() => new SseParser().push("x".repeat(1_000_001))).toThrow(SseProtocolError);
  });
});
