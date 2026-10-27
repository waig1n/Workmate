import { describe, expect, it } from "vitest";
import { taskDisplayStatus, taskRefreshInterval } from "./taskExecutionPhase";
import type { Task } from "./types";

describe("task execution presentation", () => {
  it("refines only created, leaving cancellation and terminal facts authoritative", () => {
    expect(taskDisplayStatus({ status: "created", execution_phase: "preparing" })).toBe("preparing");
    expect(taskDisplayStatus({ status: "created", execution_phase: "running" })).toBe("running");
    expect(taskDisplayStatus({ status: "created" })).toBe("created");
    for (const status of ["cancelling", "completed", "failed", "cancelled", "interrupted"] as const) {
      expect(taskDisplayStatus({ status, execution_phase: "preparing" })).toBe(status);
    }
  });
  it("refreshes while preparation has no Reply stream and stops on terminal history", () => {
    const tasks = (status: Task["status"]) => [{ status } as Task];
    expect(taskRefreshInterval(tasks("created"))).toBe(1000);
    expect(taskRefreshInterval(tasks("running"))).toBe(5000);
    expect(taskRefreshInterval(tasks("completed"))).toBe(false);
    expect(taskRefreshInterval([])).toBe(false);
    expect(taskRefreshInterval(undefined)).toBe(false);
  });
});
