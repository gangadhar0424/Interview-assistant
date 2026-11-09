import type { QaEvent } from "../shared/contracts";

export class SseProtocolError extends Error {}

export class SseParser {
  private buffer = "";

  push(chunk: string): Array<{ event: string; data: string }> {
    this.buffer += chunk;
    if (this.buffer.length > 1_000_000) {
      throw new SseProtocolError("The answer stream frame was too large.");
    }
    const frames: Array<{ event: string; data: string }> = [];
    let boundary = /\r\n\r\n|\n\n|\r\r/.exec(this.buffer);
    while (boundary) {
      const block = this.buffer.slice(0, boundary.index);
      this.buffer = this.buffer.slice(boundary.index + boundary[0].length);
      const frame = parseFrame(block);
      if (frame) frames.push(frame);
      boundary = /\r\n\r\n|\n\n|\r\r/.exec(this.buffer);
    }
    return frames;
  }
}

function parseFrame(block: string): { event: string; data: string } | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split(/\r\n|\r|\n/)) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    if (field === "data") data.push(value);
  }
  return data.length ? { event, data: data.join("\n") } : null;
}

type RecordValue = Record<string, unknown>;

function objectValue(data: string): RecordValue {
  let value: unknown;
  try {
    value = JSON.parse(data);
  } catch {
    throw new SseProtocolError("The server sent invalid event data.");
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new SseProtocolError("The server sent invalid event data.");
  }
  return value as RecordValue;
}

function string(value: RecordValue, key: string): string {
  const result = value[key];
  if (typeof result !== "string") throw new SseProtocolError("The server sent an invalid event.");
  return result;
}

function nullableString(value: RecordValue, key: string): string | null {
  const result = value[key];
  if (result === null || result === undefined) return null;
  if (typeof result !== "string") throw new SseProtocolError("The server sent an invalid event.");
  return result;
}

function integer(value: RecordValue, key: string): number {
  const result = value[key];
  if (typeof result !== "number" || !Number.isInteger(result) || result < 0) {
    throw new SseProtocolError("The server sent an invalid event.");
  }
  return result;
}

function nullableInteger(value: RecordValue, key: string): number | null {
  const result = value[key];
  if (result === null || result === undefined) return null;
  return integer(value, key);
}

export function parseSseEvent(frame: { event: string; data: string }): QaEvent | null {
  if (
    ![
      "question.created",
      "retrieval.done",
      "answer.started",
      "answer.delta",
      "answer.done",
      "answer.error",
    ].includes(frame.event)
  ) {
    return null;
  }
  const value = objectValue(frame.data);
  switch (frame.event) {
    case "question.created":
      return {
        type: frame.event,
        questionId: string(value, "question_id"),
        answerId: string(value, "answer_id"),
      };
    case "retrieval.done":
      return {
        type: frame.event,
        questionId: string(value, "question_id"),
        chunkCount: integer(value, "chunk_count"),
        ms: integer(value, "ms"),
      };
    case "answer.started":
      return {
        type: frame.event,
        answerId: string(value, "answer_id"),
        questionId: string(value, "question_id"),
        provider: string(value, "provider"),
        model: string(value, "model"),
      };
    case "answer.delta":
      return {
        type: frame.event,
        answerId: string(value, "answer_id"),
        index: integer(value, "index"),
        text: string(value, "text"),
      };
    case "answer.done": {
      const metrics = value.metrics;
      if (typeof metrics !== "object" || metrics === null || Array.isArray(metrics)) {
        throw new SseProtocolError("The server sent invalid event metrics.");
      }
      if (typeof value.fallback_used !== "boolean") {
        throw new SseProtocolError("The server sent invalid event metrics.");
      }
      const m = metrics as RecordValue;
      return {
        type: frame.event,
        answerId: string(value, "answer_id"),
        questionId: string(value, "question_id"),
        provider: string(value, "provider"),
        model: string(value, "model"),
        metrics: {
          retrieval_ms: nullableInteger(m, "retrieval_ms"),
          first_token_ms: nullableInteger(m, "first_token_ms"),
          total_ms: nullableInteger(m, "total_ms"),
        },
        fallbackUsed: value.fallback_used,
        inputTokens: nullableInteger(value, "input_tokens"),
        outputTokens: nullableInteger(value, "output_tokens"),
      };
    }
    default:
      return {
        type: "answer.error",
        answerId: nullableString(value, "answer_id"),
        questionId: nullableString(value, "question_id"),
        code: string(value, "code"),
        retryable: value.retryable === true,
      };
  }
}
