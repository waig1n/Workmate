import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  classifyRealtimeCursor, realtimeMessageId, applyTextDelta, compareStreamIds, decodeRealtimeCursor, encodeRealtimeCursor,
  parseRealtimeEnvelope, parseMessageSnapshot, parseRealtimeReset,
} from "./realtime-protocol";

const generation = "6ac09542-9b01-4c3e-b80b-d4dde297e454";
const cursor = { schema_version: 1 as const, conversation_id: "conversation", generation, stream_id: "99-1" };
function delta(text = "你好🙂", overrides = {}) {
  return { schema_version: 1, event_id: "event", conversation_id: "conversation", task_id: "task", reply_id: "reply",
    source: "worker", execution_epoch: 1, business_seq: null,
    payload: { type: "TEXT_MESSAGE_CONTENT", delta: text },
    text: { message_id: realtimeMessageId("task", "reply"), block_id: "block", content_version: 1, from_offset: 0, to_offset: new TextEncoder().encode(text).length }, ...overrides };
}
const empty = { message_id: realtimeMessageId("task", "reply"), block_id: "block", content: "", content_version: 0, end_utf8: 0, closed: false, execution_epoch: 1 };

describe("versioned realtime protocol", () => {
  it("roundtrips a conversation-bound opaque cursor", () => {
    expect(decodeRealtimeCursor(encodeRealtimeCursor(cursor), "conversation")).toEqual(cursor);
    expect(decodeRealtimeCursor(encodeRealtimeCursor(cursor), "other")).toBeNull();
    expect(decodeRealtimeCursor("product-old:1", "conversation")).toBeNull();
    expect(compareStreamIds("9-100", "10-0")).toBe(-1);
    expect(compareStreamIds("9007199254740991-0", "9007199254740990-999")).toBe(1);
  });
  it.each(["1-01", "-1-0", "18446744073709551616-0", "1-0\n"])("rejects invalid Stream IDs %s", (stream_id) => {
    expect(() => encodeRealtimeCursor({ ...cursor, stream_id })).toThrow();
  });
  it("counts UTF8 bytes consistently and does not append a duplicate twice", () => {
    const envelope = parseRealtimeEnvelope(delta());
    expect(envelope).not.toBeNull();
    const applied = applyTextDelta(empty, envelope!);
    expect(applied.action).toBe("apply");
    expect(applied.state).toMatchObject({ content: "你好🙂", content_version: 1, end_utf8: 10 });
    expect(applyTextDelta(applied.state, envelope!).action).toBe("ignore");
  });
  it.each(["\ud800", "\udfff"])("rejects lone surrogate deltas", (text) => {
    expect(parseRealtimeEnvelope(delta(text))).toBeNull();
  });
  it("rejects unknown envelope fields and coercible numbers", () => {
    expect(parseRealtimeEnvelope(delta("A", { credential: "secret" }))).toBeNull();
    expect(parseRealtimeEnvelope(delta("A", { execution_epoch: true }))).toBeNull();
  });
  it("detects missing content rather than silently appending", () => {
    const value = delta("B");
    value.text = { ...value.text, content_version: 2, from_offset: 1, to_offset: 2 };
    expect(applyTextDelta(empty, parseRealtimeEnvelope(value)!).action).toBe("reset");
  });
  it("never appends content beyond a closed final block", () => {
    const value = delta("B");
    value.text = { ...value.text, content_version: 2, from_offset: 1, to_offset: 2 };
    const state = { ...empty, content: "A", content_version: 1, end_utf8: 1, closed: true };
    expect(applyTextDelta(state, parseRealtimeEnvelope(value)!).action).not.toBe("apply");
  });
});


type GoldenFixtures = {
  reset_anchor_cases: { name: string; phase: "partial" | "approval" | "final"; anchor: string; sync_status: "synced" | "awaiting_snapshot"; valid: boolean }[];
  payload_budget_cases: { name: string; fill: string; payload_bytes: number; valid: boolean }[];
  valid_envelopes: { name: string; value: unknown }[];
  invalid_envelopes: { name: string; value: unknown }[];
  invalid_snapshots: { name: string; value: unknown }[];
  invalid_resets: { name: string; value: unknown }[];
  cursors: { value: import("./realtime-protocol").RealtimeCursor; token: string }[];
  message_ids: { task_id: string; reply_id: string; message_id: string }[];
  deltas: { name: string; state: import("./realtime-protocol").TextState; envelope: import("./realtime-protocol").RealtimeEnvelope; expected: import("./realtime-protocol").DeltaResult }[];
  snapshots: import("./realtime-protocol").MessageSnapshot[];
  resets: import("./realtime-protocol").RealtimeReset[];
};
const golden: GoldenFixtures = JSON.parse(readFileSync(resolve(process.cwd(), "../packages/contracts/fixtures/realtime_v1.json"), "utf8"));
describe("shared Python and TypeScript golden protocol fixtures", () => {
  it.each(golden.cursors)("cursor $value.conversation_id", ({ value, token }) => {
    expect(encodeRealtimeCursor(value)).toBe(token);
    expect(decodeRealtimeCursor(token, value.conversation_id)).toEqual(value);
  });
  it.each(golden.message_ids)("stable UUID5 $task_id / $reply_id", ({ task_id, reply_id, message_id }) => {
    expect(realtimeMessageId(task_id, reply_id)).toBe(message_id);
  });
  it.each(golden.deltas)("delta $name", ({ state, envelope, expected }) => {
    const parsed = parseRealtimeEnvelope(envelope);
    expect(parsed).not.toBeNull();
    expect(applyTextDelta(state, parsed!)).toEqual(expected);
  });
  it.each(golden.snapshots)("valid snapshot %#", (snapshot) => {
    expect(parseMessageSnapshot(snapshot)).toEqual(snapshot);
    expect(parseMessageSnapshot(snapshot, "wrong-conversation")).toBeNull();
  });
  it.each(golden.resets)("valid reset %#", (reset) => {
    expect(parseRealtimeReset(reset)).toEqual(reset);
    expect(parseRealtimeReset(reset, "wrong-conversation")).toBeNull();
  });
});


it("validates current cursor generation, retention window and future positions", () => {
  const bounds = { conversation_id: "conversation", generation, first_stream_id: "90-0", last_stream_id: "100-0" };
  expect(classifyRealtimeCursor(cursor, bounds).action).toBe("resume");
  expect(classifyRealtimeCursor(cursor, { ...bounds, first_stream_id: "100-0" }).reason).toBe("trimmed");
  expect(classifyRealtimeCursor(cursor, { ...bounds, generation: "00000000-0000-4000-8000-000000000001" }).reason).toBe("generation_changed");
  expect(() => classifyRealtimeCursor(cursor, { ...bounds, last_stream_id: "90-0" })).toThrow("Future");
  expect(() => classifyRealtimeCursor(cursor, { ...bounds, conversation_id: "other" })).toThrow();
  expect(compareStreamIds("18446744073709551615-18446744073709551615", "18446744073709551615-1")).toBe(1);
});

it("rejects noncanonical cursor JSON rather than ambiguously interpreting duplicate keys", () => {
  const raw = JSON.stringify({ ...cursor, schema_version: 1 });
  const token = "rt1." + btoa(raw).replace(/=/g, "");
  expect(decodeRealtimeCursor(token, "conversation")).toBeNull();
});

it("validates the entire reset before exposing any snapshots for atomic replacement", () => {
  const reset = structuredClone(golden.resets[0]);
  reset.snapshots.push(structuredClone(reset.snapshots[0]));
  expect(parseRealtimeReset(reset)).toBeNull();
  const snapshot = structuredClone(golden.snapshots[0]);
  snapshot.blocks.push(structuredClone(snapshot.blocks[0]));
  expect(parseMessageSnapshot(snapshot)).toBeNull();
});

it("rejects forged snapshot message identity and open final blocks", () => {
  const snapshot = structuredClone(golden.snapshots[0]);
  expect(parseMessageSnapshot({ ...snapshot, message_id: realtimeMessageId("other-task", "reply") })).toBeNull();
  expect(parseMessageSnapshot({ ...snapshot, phase: "final", blocks: snapshot.blocks.map((b: object) => ({ ...b, closed: false })) })).toBeNull();
});


it("rejects coercible enum arrays consistently with Python", () => {
  expect(parseRealtimeEnvelope({ schema_version: 1, event_id: "e", conversation_id: "c", source: ["business"], business_seq: 1, payload: { type: "RUN_STARTED" } })).toBeNull();
  const snapshot = golden.snapshots[0];
  expect(parseMessageSnapshot({ ...snapshot, phase: ["partial"] })).toBeNull();
  expect(parseMessageSnapshot({ ...snapshot, blocks: snapshot.blocks.map((b) => ({ ...b, kind: ["text"] })) })).toBeNull();
  expect(parseRealtimeReset({ ...golden.resets[0], reason: ["initial"] })).toBeNull();
  expect(parseRealtimeReset({ ...golden.resets[0], sync_status: ["synced"] })).toBeNull();
});


it("does not call an incomplete durable snapshot synchronized", () => {
  const reset = structuredClone(golden.resets[0]);
  reset.sync_status = "synced";
  reset.snapshots[0].incomplete = true;
  reset.snapshots[0].phase = "interrupted";
  expect(parseRealtimeReset(reset)).toBeNull();
  reset.sync_status = "incomplete";
  expect(parseRealtimeReset(reset)).not.toBeNull();
});


describe("shared strict rejection fixtures", () => {
  it.each(golden.valid_envelopes)("valid envelope $name", ({ value }) => expect(parseRealtimeEnvelope(value)).not.toBeNull());
  it.each(golden.invalid_envelopes)("invalid envelope $name", ({ value }) => expect(parseRealtimeEnvelope(value)).toBeNull());
  it.each(golden.invalid_snapshots)("invalid snapshot $name", ({ value }) => expect(parseMessageSnapshot(value)).toBeNull());
  it.each(golden.invalid_resets)("invalid reset $name", ({ value }) => expect(parseRealtimeReset(value)).toBeNull());
});


it("rejects incomplete final success but preserves explicitly interrupted partial history", () => {
  const snapshot = structuredClone(golden.snapshots[0]);
  snapshot.blocks = snapshot.blocks.map((block) => ({ ...block, closed: true }));
  snapshot.incomplete = true;
  snapshot.phase = "final";
  expect(parseMessageSnapshot(snapshot)).toBeNull();
  snapshot.phase = "interrupted";
  expect(parseMessageSnapshot(snapshot)).toEqual(snapshot);
});


it.each(golden.payload_budget_cases)("shared payload budget $name reserves metadata", (spec) => {
  const payload = { type: "CUSTOM", name: "RESULT", value: "" };
  const encode = (value: unknown) => new TextEncoder().encode(JSON.stringify(value));
  const remaining = spec.payload_bytes - encode(payload).length;
  const width = encode(spec.fill).length - 2;
  payload.value = spec.fill.repeat(Math.floor(remaining / width)) + "x".repeat(remaining % width);
  expect(encode(payload).length).toBe(spec.payload_bytes);
  const parsed = parseRealtimeEnvelope({ event_id: "e".repeat(128), conversation_id: "c".repeat(128),
    task_id: "t".repeat(128), reply_id: "r".repeat(128), source: "business", business_seq: 1, payload });
  if (spec.valid) {
    expect(parsed).not.toBeNull();
    expect(parsed?.payload).toEqual(payload);
    expect(encode(parsed).length).toBeGreaterThan(262144);
    expect(encode(parsed).length).toBeLessThanOrEqual(266240);
  } else expect(parsed).toBeNull();
});


it.each(golden.reset_anchor_cases)("shared reset anchor $name", (spec) => {
  const value = structuredClone(golden.resets[0]);
  value.snapshots[0].phase = spec.phase;
  value.sync_status = spec.sync_status;
  if (spec.anchor === "none") value.snapshots[0].covered_cursor = null;
  else if (spec.anchor === "old_generation") value.generation = "00000000-0000-4000-8000-000000000002";
  const parsed = parseRealtimeReset(value);
  if (!spec.valid) expect(parsed).toBeNull();
  else {
    expect(parsed).toEqual(value);
    expect(parsed?.snapshots[0].incomplete).toBe(false);
    expect(parsed?.snapshots[0].blocks[0].text).toBe(value.snapshots[0].blocks[0].text);
    expect(parsed?.reason).toBe("initial");
  }
});
