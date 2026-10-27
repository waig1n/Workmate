export interface MergeTextItem {
  kind: "text";
  id: string;
  role: string;
  text: string;
  done: boolean;
  taskId?: string;
  runId?: string | null;
}

export function historyWithReplyIds<H extends MergeTextItem>(
  history: readonly H[], tasks: readonly { id: string; run_id: string | null }[],
): Array<H & { runId?: string | null }> {
  const replies = new Map(tasks.map((task) => [task.id, task.run_id]));
  return history.map((item) => ({ ...item, runId: item.runId ?? (item.taskId ? replies.get(item.taskId) : undefined) }));
}

export function mergeHistoryWithReplay<
  H extends MergeTextItem,
  I extends {kind: string; id: string},
>(history: readonly H[], replay: readonly I[]): Array<H | I> {
  // Replay is a suffix of the in-memory event buffer. One persisted
  // assistant message is the concatenation of every text block the run
  // emitted between tool calls, but replay splits them into one item
  // per block - so a multi-block run's replay items ("b1","b2") never
  // textually match the persisted message ("b1b2"). Walk both lists
  // newest-first and match a persisted message against the RAW
  // concatenation of a trailing run of completed replay blocks: the
  // comparison trims the joined whole (exactly how the persisted text
  // was produced - raw join, then one strip), never the individual
  // blocks, so whitespace between and around blocks survives.
  //
  // Known Task/Reply identities constrain every match. Text comparison is
  // retained for older records without identities and for multi-block replies.
  const blocks: { replayIndex: number; text: string; turn: number; taskId?: string; runId?: string }[] = [];
  let turn = 0;
  for (let replayIndex = 0; replayIndex < replay.length; replayIndex += 1) {
    const item = replay[replayIndex];
    // A new user message separates replies even in older identity-free data.
    if (item.kind === "text" && "role" in item && item.role === "user") turn += 1;
    if (
      item.kind === "text"
      && "role" in item
      && (item as {role: unknown}).role === "assistant"
      && "text" in item
      && typeof (item as {text: unknown}).text === "string"
      && "done" in item
      && (item as {done: unknown}).done === true
    ) {
      blocks.push({ replayIndex, text: (item as {text: string}).text, turn,
        taskId: "taskId" in item && typeof item.taskId === "string" ? item.taskId : undefined,
        runId: "runId" in item && typeof item.runId === "string" ? item.runId : undefined });
    }
  }

  // Each matched historical assistant message is replaced by the replay
  // slice that produced it. Replacing in place is essential: filtering all
  // historical answers and appending replay wholesale changes
  // [user, assistant, user, assistant] into
  // [user, user, assistant, assistant] after a refresh.
  const matches = new Map<number, { fromBlock: number; toBlock: number }>();
  // Index into ``blocks``: start of the (exclusive) unconsumed tail.
  let tailStart = blocks.length;
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const item = history[index];
    if (item.role !== "assistant") continue;
    const value = item.text.trim();
    if (!value) continue;

    // Match the SHORTEST trailing run of blocks whose RAW join trims
    // to the persisted text: extend backwards and stop at the first
    // match - the message boundary. Once the raw join trims to more
    // than the value it can never shrink back, so extending further
    // would only waste work.
    let matchedFrom = -1;
    let matchedTo = tailStart;
    // Live may now contain a later completed reply absent from the REST
    // snapshot. Search earlier endings too, leaving the new tail untouched.
    for (let end = tailStart; end > 0 && matchedFrom < 0; end -= 1) {
      const last = blocks[end - 1];
      let joined = "";
      let taskId = item.taskId;
      let runId = item.runId;
      for (let consumed = end - 1; consumed >= 0; consumed -= 1) {
        const block = blocks[consumed];
        if (block.turn !== last.turn
          || (taskId && block.taskId && taskId !== block.taskId)
          || (runId && block.runId && runId !== block.runId)) break;
        // Retain identities from every visited block: an anonymous tail must
        // not bridge two earlier blocks with conflicting Task/Reply identity.
        taskId ||= block.taskId;
        runId ||= block.runId;
        joined = block.text + joined;
        if (joined.trim() === value) {
          matchedFrom = consumed;
          matchedTo = end;
          break;
        }
        if (joined.trim().length > value.length) break;
      }
    }
    if (matchedFrom < 0) continue;
    matches.set(index, { fromBlock: matchedFrom, toBlock: matchedTo });
    tailStart = matchedFrom;
  }

  const merged: Array<H | I> = [];
  let replayCursor = 0;
  for (let index = 0; index < history.length; index += 1) {
    const match = matches.get(index);
    if (!match) {
      merged.push(history[index]);
      continue;
    }

    // Include tool/reasoning items that precede or occur between the
    // matched text blocks so a run summary remains beside its answer.
    const lastBlock = blocks[match.toBlock - 1];
    const replayEnd = lastBlock.replayIndex + 1;
    merged.push(...replay.slice(replayCursor, replayEnd));
    replayCursor = replayEnd;
  }
  merged.push(...replay.slice(replayCursor));
  return merged;
}
