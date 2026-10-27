/** Versioned realtime presentation protocol; grants no execution or access rights. */
export type RealtimeCursor = { schema_version: 1; conversation_id: string; generation: string; stream_id: string };
export type TextDelta = { message_id: string; block_id: string; content_version: number; from_offset: number; to_offset: number };
export type RealtimeEnvelope = {
  schema_version: 1; event_id: string; conversation_id: string; task_id: string | null; reply_id: string | null;
  source: "worker" | "business"; execution_epoch: number | null; business_seq: number | null;
  payload: Record<string, unknown>; text: TextDelta | null;
};
export type ContentBlock = { block_id: string; kind: "text" | "reasoning"; text: string; content_version: number; closed: boolean };
export type MessageSnapshot = {
  schema_version: 1; message_id: string; conversation_id: string; task_id: string; reply_id: string;
  execution_epoch: number; snapshot_version: number; blocks: ContentBlock[]; covered_cursor: RealtimeCursor | null;
  business_seq: number; phase: "partial" | "approval" | "final" | "interrupted"; incomplete: boolean; process_end: number | null;
};
export type RealtimeReset = {
  schema_version: 1; conversation_id: string; generation: string;
  reason: "initial" | "generation_changed" | "trimmed" | "legacy_cursor" | "content_gap" | "log_lost" | "slow_client";
  snapshots: MessageSnapshot[]; business_state: Record<string, unknown>; business_seq: number;
  sync_status: "synced" | "awaiting_snapshot" | "incomplete";
};
export type TextState = { message_id: string; block_id: string; content: string; content_version: number; end_utf8: number; closed: boolean; execution_epoch: number };
export type DeltaResult = { action: "apply" | "ignore" | "reset" | "reject"; state: TextState; reason: string | null };
const encoder = new TextEncoder();
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const uuid5 = /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const streamId = /^(0|[1-9][0-9]*)-(0|[1-9][0-9]*)$/;
function object(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === "object" && !Array.isArray(value); }
function fields(value: Record<string, unknown>, required: string[], optional: string[] = []): boolean {
  return required.every((key) => Object.hasOwn(value, key)) && Object.keys(value).every((key) => required.includes(key) || optional.includes(key));
}
function text(value: unknown): value is string {
  if (typeof value !== "string" || value.includes("\0")) return false;
  for (let i = 0; i < value.length; i++) {
    const c = value.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) { const next = value.charCodeAt(++i); if (!(next >= 0xdc00 && next <= 0xdfff)) return false; }
    else if (c >= 0xdc00 && c <= 0xdfff) return false;
  }
  return true;
}
function identity(value: unknown): value is string { return text(value) && value.length > 0 && Array.from(value).length <= 128; }
function integer(value: unknown, minimum = 0): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum; }
function jsonValue(value: unknown, depth = 0): boolean {
  if (depth > 64) return false;
  if (value === null || typeof value === "boolean") return true;
  if (typeof value === "string") return text(value);
  if (typeof value === "number") return Number.isFinite(value) && Math.abs(value) <= Number.MAX_SAFE_INTEGER;
  if (Array.isArray(value)) return value.every((v) => jsonValue(v, depth + 1));
  return object(value) && Object.entries(value).every(([k, v]) => text(k) && jsonValue(v, depth + 1));
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (object(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}
function validStreamId(value: unknown): value is string {
  return typeof value === "string" && value.length <= 41 && streamId.test(value)
    && value.split("-").every((v) => BigInt(v) <= 18446744073709551615n);
}
export function compareStreamIds(a: string, b: string): -1 | 0 | 1 {
  if (!validStreamId(a) || !validStreamId(b)) throw new Error("Invalid stream ID");
  const left = a.split("-").map(BigInt), right = b.split("-").map(BigInt);
  for (let i = 0; i < 2; i++) { if (left[i] < right[i]) return -1; if (left[i] > right[i]) return 1; }
  return 0;
}
/** UUIDv5 presentation identity, matching Python uuid.NAMESPACE_URL. */
export function realtimeMessageId(taskId: string, replyId: string): string {
  if (!identity(taskId) || !identity(replyId)) throw new Error("Invalid message identity");
  const namespace = "6ba7b8119dad11d180b400c04fd430c8";
  const prefix = Uint8Array.from(namespace.match(/../g)!, (pair) => parseInt(pair, 16));
  const name = encoder.encode("workmate:message:" + JSON.stringify([taskId, replyId]));
  const data = new Uint8Array(prefix.length + name.length); data.set(prefix); data.set(name, prefix.length);
  const bytes = new Uint8Array(Math.ceil((data.length + 9) / 64) * 64); bytes.set(data); bytes[data.length] = 0x80;
  const view = new DataView(bytes.buffer); view.setUint32(bytes.length - 4, data.length * 8);
  let h0 = 0x67452301, h1 = 0xefcdab89, h2 = 0x98badcfe, h3 = 0x10325476, h4 = 0xc3d2e1f0;
  const rotate = (v: number, n: number) => (v << n) | (v >>> (32 - n));
  for (let offset = 0; offset < bytes.length; offset += 64) {
    const words = new Uint32Array(80);
    for (let i = 0; i < 16; i++) words[i] = view.getUint32(offset + i * 4);
    for (let i = 16; i < 80; i++) words[i] = rotate(words[i - 3] ^ words[i - 8] ^ words[i - 14] ^ words[i - 16], 1);
    let a = h0, b = h1, c = h2, d = h3, e = h4;
    for (let i = 0; i < 80; i++) {
      const f = i < 20 ? (b & c) | (~b & d) : i < 40 ? b ^ c ^ d : i < 60 ? (b & c) | (b & d) | (c & d) : b ^ c ^ d;
      const k = i < 20 ? 0x5a827999 : i < 40 ? 0x6ed9eba1 : i < 60 ? 0x8f1bbcdc : 0xca62c1d6;
      const next = (rotate(a, 5) + f + e + k + words[i]) >>> 0;
      e = d; d = c; c = rotate(b, 30); b = a; a = next;
    }
    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0; h4 = (h4 + e) >>> 0;
  }
  const digest = [h0, h1, h2, h3, h4].map((n) => n.toString(16).padStart(8, "0")).join("").slice(0, 32).split("");
  digest[12] = "5"; digest[16] = ((parseInt(digest[16], 16) & 3) | 8).toString(16);
  const hex = digest.join(""); return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
function parseCursor(value: unknown): RealtimeCursor | null {
  if (object(value) && !Object.hasOwn(value, "schema_version")) value = { schema_version: 1, ...value };
  if (!object(value) || !fields(value, ["schema_version", "conversation_id", "generation", "stream_id"])
    || value.schema_version !== 1 || !identity(value.conversation_id) || typeof value.generation !== "string"
    || !uuid.test(value.generation) || !validStreamId(value.stream_id)) return null;
  return value as RealtimeCursor;
}
export function encodeRealtimeCursor(value: RealtimeCursor): string {
  if (!parseCursor(value)) throw new Error("Invalid realtime cursor");
  const encoded = encoder.encode(canonical(value));
  const token = "rt1." + btoa(Array.from(encoded, (byte) => String.fromCharCode(byte)).join("")).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
  if (token.length > 2048) throw new Error("Cursor too long");
  return token;
}
export function decodeRealtimeCursor(token: unknown, conversationId: string): RealtimeCursor | null {
  if (typeof token !== "string" || token.length > 2048 || !/^rt1\.[A-Za-z0-9_-]+$/.test(token)) return null;
  try {
    const body = token.slice(4).replace(/-/g, "+").replace(/_/g, "/");
    const bytes = Uint8Array.from(atob(body), (char) => char.charCodeAt(0));
    const parsed = parseCursor(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)));
    return parsed && parsed.conversation_id === conversationId && encodeRealtimeCursor(parsed) === token ? parsed : null;
  } catch { return null; }
}
function parseDelta(value: unknown): TextDelta | null {
  if (!object(value) || !fields(value, ["message_id", "block_id", "content_version", "from_offset", "to_offset"])
    || !identity(value.message_id) || !identity(value.block_id) || !integer(value.content_version, 1)
    || !integer(value.from_offset) || !integer(value.to_offset) || value.to_offset < value.from_offset) return null;
  return value as TextDelta;
}
export function parseRealtimeEnvelope(value: unknown, conversationId?: string): RealtimeEnvelope | null {
  if (object(value) && !Object.hasOwn(value, "schema_version")) value = { schema_version: 1, ...value };
  if (!object(value) || !fields(value, ["schema_version", "event_id", "conversation_id", "source", "payload"],
    ["task_id", "reply_id", "execution_epoch", "business_seq", "text"]) || !jsonValue(value)
    || value.schema_version !== 1 || !identity(value.event_id) || !identity(value.conversation_id)
    || (conversationId !== undefined && value.conversation_id !== conversationId)
    || !object(value.payload) || !(text(value.payload.type) && value.payload.type.length > 0) || !(typeof value.source === "string" && ["worker", "business"].includes(value.source))) return null;
  const normalized: Record<string, unknown> = { task_id: null, reply_id: null, execution_epoch: null, business_seq: null, text: null, ...value };
  if ((normalized.task_id !== null && !identity(normalized.task_id)) || (normalized.reply_id !== null && !identity(normalized.reply_id))) return null;
  if (normalized.source === "worker") {
    if (!identity(normalized.task_id) || !identity(normalized.reply_id) || !integer(normalized.execution_epoch, 1) || normalized.business_seq !== null) return null;
  } else if (!integer(normalized.business_seq, 1) || normalized.execution_epoch !== null) return null;
  const isText = ["TEXT_MESSAGE_CONTENT", "REASONING_MESSAGE_CONTENT"].includes(value.payload.type as string);
  if (isText) {
    const metadata = parseDelta(normalized.text);
    if (normalized.source !== "worker" || !metadata || metadata.message_id !== realtimeMessageId(normalized.task_id as string, normalized.reply_id as string) || !text(value.payload.delta)
      || encoder.encode(value.payload.delta).length !== metadata.to_offset - metadata.from_offset) return null;
  } else if (normalized.text !== null) return null;
  // Preserve the Product payload ceiling and reserve 4 KiB for wire metadata.
  if (encoder.encode(canonical(value.payload)).length > 256 * 1024) return null;
  if (encoder.encode(canonical(normalized)).length > 260 * 1024) return null;
  return normalized as RealtimeEnvelope;
}
export function parseMessageSnapshot(value: unknown, conversationId?: string): MessageSnapshot | null {
  if (object(value) && !Object.hasOwn(value, "schema_version")) value = { schema_version: 1, ...value };
  if (!object(value) || !fields(value, ["schema_version", "message_id", "conversation_id", "task_id", "reply_id", "execution_epoch", "snapshot_version", "blocks", "business_seq", "phase", "incomplete"], ["covered_cursor", "process_end"])
    || !jsonValue(value) || value.schema_version !== 1 || typeof value.message_id !== "string" || !uuid5.test(value.message_id)
    || !identity(value.conversation_id) || !identity(value.task_id) || !identity(value.reply_id)
    || (conversationId !== undefined && value.conversation_id !== conversationId)
    || !integer(value.execution_epoch, 1) || !integer(value.snapshot_version, 1) || !integer(value.business_seq)
    || !(typeof value.phase === "string" && ["partial", "approval", "final", "interrupted"].includes(value.phase)) || typeof value.incomplete !== "boolean"
    || !Array.isArray(value.blocks)) return null;
  if (value.message_id !== realtimeMessageId(value.task_id, value.reply_id)) return null;
  if (value.phase === "final" && value.incomplete) return null;
  const ids = new Set<string>();
  for (const block of value.blocks) {
    if (!object(block) || !fields(block, ["block_id", "kind", "text", "content_version", "closed"])
      || !identity(block.block_id) || ids.has(block.block_id) || !(typeof block.kind === "string" && ["text", "reasoning"].includes(block.kind))
      || !text(block.text) || !integer(block.content_version) || typeof block.closed !== "boolean"
      || (["final", "interrupted"].includes(value.phase as string) && !block.closed)) return null;
    ids.add(block.block_id);
  }
  const covered = value.covered_cursor == null ? null : parseCursor(value.covered_cursor);
  if (value.covered_cursor != null && (!covered || covered.conversation_id !== value.conversation_id)) return null;
  if (value.process_end != null && !integer(value.process_end)) return null;
  return { ...value, covered_cursor: covered, process_end: value.process_end ?? null } as MessageSnapshot;
}
export function parseRealtimeReset(value: unknown, conversationId?: string): RealtimeReset | null {
  if (object(value) && !Object.hasOwn(value, "schema_version")) value = { schema_version: 1, ...value };
  if (!object(value) || !fields(value, ["schema_version", "conversation_id", "generation", "reason", "snapshots", "business_state", "business_seq", "sync_status"])
    || !jsonValue(value) || value.schema_version !== 1 || !identity(value.conversation_id)
    || (conversationId !== undefined && value.conversation_id !== conversationId)
    || typeof value.generation !== "string" || !uuid.test(value.generation)
    || !(typeof value.reason === "string" && ["initial", "generation_changed", "trimmed", "legacy_cursor", "content_gap", "log_lost", "slow_client"].includes(value.reason))
    || !(typeof value.sync_status === "string" && ["synced", "awaiting_snapshot", "incomplete"].includes(value.sync_status))
    || !Array.isArray(value.snapshots) || !object(value.business_state) || !integer(value.business_seq)) return null;
  const snapshots: MessageSnapshot[] = [], ids = new Set<string>();
  for (const raw of value.snapshots) {
    const snapshot = parseMessageSnapshot(raw, value.conversation_id);
    if (!snapshot || ids.has(snapshot.message_id) || snapshot.business_seq > value.business_seq
      || (value.sync_status === "synced" && snapshot.incomplete)
      || (value.sync_status === "synced" && ["partial", "approval"].includes(snapshot.phase)
        && (!snapshot.covered_cursor || snapshot.covered_cursor.generation !== value.generation))) return null;
    ids.add(snapshot.message_id); snapshots.push(snapshot);
  }
  return { ...value, snapshots } as RealtimeReset;
}
export function applyTextDelta(state: TextState, event: RealtimeEnvelope): DeltaResult {
  const result = (action: DeltaResult["action"], reason: string): DeltaResult => ({ action, state, reason });
  if (!text(state.content) || !integer(state.content_version) || !integer(state.end_utf8) || !integer(state.execution_epoch, 1)
    || encoder.encode(state.content).length !== state.end_utf8 || typeof state.closed !== "boolean") return result("reject", "invalid_state");
  const delta = event.text;
  if (event.source !== "worker" || !delta || typeof event.payload.delta !== "string") return result("reject", "not_text_delta");
  if (state.message_id !== delta.message_id || state.block_id !== delta.block_id) return result("reject", "identity_mismatch");
  if (event.execution_epoch! < state.execution_epoch) return result("ignore", "stale_epoch");
  if (state.closed) return result("ignore", "closed");
  if (delta.content_version < state.content_version) return result("ignore", "stale_version");
  if (delta.content_version > state.content_version + 1) return result("reset", "version_gap");
  if (delta.from_offset > state.end_utf8) return result("reset", "offset_gap");
  const old = encoder.encode(state.content), added = encoder.encode(event.payload.delta);
  const overlap = Math.min(state.end_utf8 - delta.from_offset, added.length);
  try {
    const decoder = new TextDecoder("utf-8", { fatal: true });
    decoder.decode(old.slice(0, delta.from_offset)); decoder.decode(old.slice(0, delta.from_offset + overlap));
  } catch { return result("reject", "invalid_utf8_boundary"); }
  for (let i = 0; i < overlap; i++) if (old[delta.from_offset + i] !== added[i]) return result("reset", "content_conflict");
  if (delta.content_version === state.content_version) return delta.to_offset <= state.end_utf8
    ? result("ignore", "duplicate") : result("reset", "version_conflict");
  let content: string;
  try { content = state.content + new TextDecoder("utf-8", { fatal: true }).decode(added.slice(overlap)); }
  catch { return result("reject", "invalid_utf8_boundary"); }
  return { action: "apply", reason: null, state: { ...state, content, content_version: delta.content_version,
    end_utf8: encoder.encode(content).length, execution_epoch: event.execution_epoch! } };
}

/** Validate an already decoded cursor against an authoritative stream window. */
export function classifyRealtimeCursor(cursor: RealtimeCursor, bounds: {
  conversation_id: string; generation: string; first_stream_id: string | null; last_stream_id: string | null;
}): { action: "resume" | "reset"; reason: "generation_changed" | "trimmed" | null; cursor: RealtimeCursor } {
  if (!parseCursor(cursor) || cursor.conversation_id !== bounds.conversation_id || !uuid.test(bounds.generation)) throw new Error("Invalid cursor scope");
  const last = bounds.last_stream_id ?? "0-0";
  if (!validStreamId(last) || (bounds.first_stream_id !== null
    && (!validStreamId(bounds.first_stream_id) || compareStreamIds(bounds.first_stream_id, last) > 0))) throw new Error("Invalid stream bounds");
  if (cursor.generation !== bounds.generation) return { action: "reset", reason: "generation_changed", cursor };
  if (compareStreamIds(cursor.stream_id, last) > 0) throw new Error("Future realtime cursor");
  if (bounds.first_stream_id !== null && compareStreamIds(cursor.stream_id, bounds.first_stream_id) < 0) return { action: "reset", reason: "trimmed", cursor };
  return { action: "resume", reason: null, cursor };
}
