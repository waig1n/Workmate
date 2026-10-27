import type { Task } from "@/lib/types";

/** Execution progress refines created only; Stop and terminal facts always win. */
export function taskDisplayStatus(task: Pick<Task, "status" | "execution_phase">) {
  return task.status === "created" && (task.execution_phase === "preparing" || task.execution_phase === "running")
    ? task.execution_phase : task.status;
}

/** Poll preparation briefly because no Reply stream exists yet. */
export function taskRefreshInterval(tasks: readonly Task[] | undefined): number | false {
  if (tasks?.some((task) => task.status === "created")) return 1000;
  return tasks?.some((task) => ["running", "awaiting_permission", "cancelling"].includes(task.status)) ? 5000 : false;
}
