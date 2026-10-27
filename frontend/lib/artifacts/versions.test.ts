import { describe, expect, it } from "vitest";

import type { Artifact } from "@/lib/types";
import { groupArtifactVersions } from "./versions";

function artifact(id: string, name: string, version: number, createdAt: string): Artifact {
  return {
    id,
    task_id: `task-${id}`,
    conversation_id: "conversation-1",
    name,
    mime_type: "text/markdown",
    size: 12,
    version,
    change_type: version === 1 ? "created" : "modified",
    metadata: {},
    created_at: createdAt,
  };
}

describe("groupArtifactVersions", () => {
  it.each([false, true])("uses the newest delivery regardless of arrival order (%s)", (reverse) => {
    const first = artifact("report-v1", "report.md", 1, "2026-09-01T00:00:00Z");
    const repeated = { ...first, task_id: "task-next", created_at: "2026-09-03T00:00:00Z" };
    const other = artifact("notes-v1", "notes.md", 1, "2026-09-02T00:00:00Z");
    const deliveries = [first, other, repeated];
    const groups = groupArtifactVersions(reverse ? deliveries.reverse() : deliveries);
    expect(groups.map((group) => group.name)).toEqual(["report.md", "notes.md"]);
    expect(groups[0].latest.task_id).toBe("task-next");
    expect(groups[0].versions).toHaveLength(1);
  });

  it("lists a reused artifact version once across task deliveries", () => {
    const original = artifact("report-v1", "report.md", 1, "2026-09-01T00:00:00Z");
    const redelivery = { ...original, task_id: "task-next", change_type: "presented" };
    const groups = groupArtifactVersions([original, redelivery]);

    expect(groups).toHaveLength(1);
    expect(groups[0].versions.map((item) => item.id)).toEqual(["report-v1"]);
    expect(groups[0].latest.id).toBe("report-v1");
  });

  it("groups exact output paths and selects the highest version", () => {
    const groups = groupArtifactVersions([
      artifact("report-v1", "report.md", 1, "2026-09-01T00:00:00Z"),
      artifact("notes-v1", "notes.md", 1, "2026-09-02T00:00:00Z"),
      artifact("report-v2", "report.md", 2, "2026-09-03T00:00:00Z"),
    ]);

    expect(groups.map((group) => group.name)).toEqual(["report.md", "notes.md"]);
    expect(groups[0].latest.id).toBe("report-v2");
    expect(groups[0].versions.map((item) => item.version)).toEqual([2, 1]);
  });

  it("does not merge a renamed file into the old filename series", () => {
    const groups = groupArtifactVersions([
      artifact("deck-v1", "deck.pptx", 1, "2026-09-01T00:00:00Z"),
      artifact("deck-renamed", "deck-v2.pptx", 1, "2026-09-02T00:00:00Z"),
    ]);

    expect(groups).toHaveLength(2);
  });
});
