import assert from "node:assert/strict";
import { test } from "vitest";

import { historyWithReplyIds, mergeHistoryWithReplay } from "./messageMerge.ts";

const text = (
  id: string,
  value: string,
  done = true,
  role = "assistant",
) => ({kind: "text" as const, id, role, text: value, done});

test("one replay removes only one of two identical historical replies", () => {
  const merged = mergeHistoryWithReplay(
    [text("db-1", "same"), text("db-2", "same")],
    [text("stream-2", "same")],
  );

  assert.deepEqual(merged.map((item) => item.id), ["db-1", "stream-2"]);
});

test("a newly completed live reply does not duplicate the earlier persisted answer", () => {
  const merged = mergeHistoryWithReplay(
    [text("db-user", "first question", true, "user"), text("db-first", "first answer")],
    [
      { ...text("live-first", "first answer"), runId: "reply-1" },
      text("live-user", "second question", true, "user"),
      { ...text("live-second", "second answer"), runId: "reply-2" },
    ],
  );
  assert.deepEqual(merged.map((item) => item.id), ["db-user", "live-first", "live-user", "live-second"]);
});

test("history matching cannot concatenate completed blocks from different replies", () => {
  const merged = mergeHistoryWithReplay(
    [text("db-answer", "firstsecond")],
    [
      { ...text("live-first", "first"), runId: "reply-1" },
      { ...text("live-second", "second"), runId: "reply-2" },
    ],
  );
  assert.deepEqual(merged.map((item) => item.id), ["db-answer", "live-first", "live-second"]);
});

test("a user message is a hard boundary even when replay replies have no identity", () => {
  const merged = mergeHistoryWithReplay([text("db", "firstsecond")], [
    text("first", "first"), text("question", "next", true, "user"), text("second", "second"),
  ]);
  assert.deepEqual(merged.map((item) => item.id), ["db", "first", "question", "second"]);
});

test.each(["runId", "taskId"] as const)("an unidentified trailing block cannot bridge distinct known %s identities", (identity) => {
  const merged = mergeHistoryWithReplay([text("db", "firstsecondthird")], [
    { ...text("first", "first"), [identity]: "one" },
    { ...text("second", "second"), [identity]: "two" },
    text("third", "third"),
  ]);
  assert.deepEqual(merged.map((item) => item.id), ["db", "first", "second", "third"]);
});

test("same-reply blocks still join across tool and reasoning activity", () => {
  const merged = mergeHistoryWithReplay([text("db", "firstsecond")], [
    { ...text("first", "first"), runId: "reply" },
    { kind: "tool", id: "tool" }, { kind: "reasoning", id: "reason" },
    { ...text("second", "second"), runId: "reply" },
  ]);
  assert.deepEqual(merged.map((item) => item.id), ["first", "tool", "reason", "second"]);
});

test("identical new text cannot replace history belonging to another known task", () => {
  const merged = mergeHistoryWithReplay(
    [{ ...text("db-first", "same"), taskId: "task-1" }, text("db-user-second", "next", true, "user")],
    [
      { ...text("live-first", "same"), taskId: "task-1" },
      { ...text("live-second", "same"), taskId: "task-2" },
    ],
  );
  assert.deepEqual(merged.map((item) => item.id), ["live-first", "db-user-second", "live-second"]);
});

test("task records link historical messages to the correct identical replay reply", () => {
  const history = [{ ...text("db-first", "same"), taskId: "task-1" },
    text("db-user-second", "next", true, "user")];
  const mapped = historyWithReplyIds(history, [{ id: "task-1", run_id: "reply-1" }]);
  assert.equal(mapped[0].runId, "reply-1");
  assert.equal("runId" in history[0], false);
  const merged = mergeHistoryWithReplay(mapped, [
    { ...text("live-first", "same"), runId: "reply-1" },
    { ...text("live-second", "same"), runId: "reply-2" },
  ]);
  assert.deepEqual(merged.map((item) => item.id), ["live-first", "db-user-second", "live-second"]);
});

test("an explicitly different reply never replaces a same-text historical answer", () => {
  const merged = mergeHistoryWithReplay(
    [{ ...text("db-first", "same"), runId: "reply-1" }],
    [{ ...text("live-second", "same"), runId: "reply-2" }],
  );
  assert.deepEqual(merged.map((item) => item.id), ["db-first", "live-second"]);
});

test("an unfinished streamed reply does not remove persisted history", () => {
  const merged = mergeHistoryWithReplay(
    [text("db-1", "answer")],
    [text("stream-active", "answer", false)],
  );

  assert.deepEqual(
    merged.map((item) => item.id),
    ["db-1", "stream-active"],
  );
});

test("a multi-block reply matches its concatenated persisted message", () => {
  // The run answered, called a tool, then answered again: replay holds
  // two blocks, the DB one concatenated message. Refresh must not
  // render both.
  const merged = mergeHistoryWithReplay(
    [text("db-1", "first answersecond answer")],
    [
      text("stream-1", "first answer"),
      text("stream-2", "second answer"),
    ],
  );

  assert.deepEqual(merged.map((item) => item.id), [
    "stream-1",
    "stream-2",
  ]);
});

test("partial block concatenation does not consume unrelated history", () => {
  // A replay block that starts a persisted message but cannot complete
  // it must not swallow the blocks belonging to a following message.
  const merged = mergeHistoryWithReplay(
    [text("db-1", "unrelated")],
    [text("stream-1", "first answer")],
  );

  assert.deepEqual(merged.map((item) => item.id), [
    "db-1",
    "stream-1",
  ]);
});

test("newline boundary between blocks matches the persisted message", () => {
  // The persisted text is the raw join stripped as a whole; the block
  // keeps its trailing "\n\n".
  const merged = mergeHistoryWithReplay(
    [text("db-1", "first answer\n\nsecond answer")],
    [
      text("stream-1", "first answer\n\n"),
      text("stream-2", "second answer"),
    ],
  );

  assert.deepEqual(merged.map((item) => item.id), [
    "stream-1",
    "stream-2",
  ]);
});

test("space boundary between blocks matches the persisted message", () => {
  const merged = mergeHistoryWithReplay(
    [text("db-1", "partial sentence continues here")],
    [
      text("stream-1", "partial sentence "),
      text("stream-2", "continues here"),
    ],
  );

  assert.deepEqual(merged.map((item) => item.id), [
    "stream-1",
    "stream-2",
  ]);
});

test("two runs with separate persisted messages dedupe independently", () => {
  // Common cross-run layout: each run produced its own persisted
  // message; both match their own blocks without cross-consumption.
  const merged = mergeHistoryWithReplay(
    [text("db-1", "run one answer"), text("db-2", "run two answer")],
    [
      text("stream-1", "run one answer"),
      text("stream-2", "run two answer"),
    ],
  );

  assert.deepEqual(merged.map((item) => item.id), [
    "stream-1",
    "stream-2",
  ]);
});

test("replayed replies keep their position after each historical user message", () => {
  const merged = mergeHistoryWithReplay(
    [
      text("db-u1", "first question", true, "user"),
      text("db-a1", "first answer"),
      text("db-u2", "second question", true, "user"),
      text("db-a2", "second answer"),
    ],
    [
      text("stream-a1", "first answer"),
      text("stream-a2", "second answer"),
    ],
  );

  assert.deepEqual(merged.map((item) => item.id), [
    "db-u1",
    "stream-a1",
    "db-u2",
    "stream-a2",
  ]);
});

test("tool activity stays immediately before its replayed reply", () => {
  const tool = { kind: "tool", id: "tool-2" };
  const merged = mergeHistoryWithReplay(
    [
      text("db-u1", "first question", true, "user"),
      text("db-a1", "first answer"),
      text("db-u2", "second question", true, "user"),
      text("db-a2", "second answer"),
    ],
    [
      text("stream-a1", "first answer"),
      tool,
      text("stream-a2", "second answer"),
    ],
  );

  assert.deepEqual(merged.map((item) => item.id), [
    "db-u1",
    "stream-a1",
    "db-u2",
    "tool-2",
    "stream-a2",
  ]);
});

test("leading whitespace stripped by the persisted whole-text is tolerated", () => {
  const merged = mergeHistoryWithReplay(
    [text("db-1", "answer after break")],
    [
      text("stream-1", "\n\nanswer after "),
      text("stream-2", "break"),
    ],
  );

  assert.deepEqual(merged.map((item) => item.id), [
    "stream-1",
    "stream-2",
  ]);
});

test("trailing whitespace in the last block matches the trimmed persisted message", () => {
  // The persisted text was produced as raw-join-then-strip; the last
  // replay block may carry trailing whitespace the strip removed.
  const merged = mergeHistoryWithReplay(
    [text("db-1", "firstsecond")],
    [
      text("stream-1", "first"),
      text("stream-2", "second\n\n"),
    ],
  );

  assert.deepEqual(merged.map((item) => item.id), [
    "stream-1",
    "stream-2",
  ]);
});
