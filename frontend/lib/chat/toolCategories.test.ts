import { describe, expect, it } from "vitest";

import type { ChatToolMsg } from "./model";
import {
  classifyTool,
  failureSummary,
  failedTools,
  summarizeTools,
} from "./toolCategories";

function tool(name: string, state = "success"): ChatToolMsg {
  return {
    kind: "tool",
    id: `tool-${name}`,
    call: { toolCallId: `tool-${name}`, toolCallName: name, args: "", result: "", state },
  };
}

describe("classifyTool", () => {
  it("maps known tool names to categories", () => {
    expect(classifyTool("Read")).toBe("read");
    expect(classifyTool("read_file")).toBe("read");
    expect(classifyTool("Bash")).toBe("command");
    expect(classifyTool("Write")).toBe("write");
    expect(classifyTool("Edit")).toBe("write");
    expect(classifyTool("skill_search")).toBe("skill");
    expect(classifyTool("mcp__github__create_issue")).toBe("mcp");
    expect(classifyTool("present_files")).toBe("other");
    expect(classifyTool("")).toBe("other");
  });
});

describe("summarizeTools", () => {
  it("counts per category and skips empty categories", () => {
    const summary = summarizeTools([
      tool("Read"),
      tool("Read"),
      tool("read_file"),
      tool("Bash"),
      tool("mcp__jira__x"),
    ]);
    expect(summary).toEqual([
      "阅读 3 个文件",
      "执行 1 个命令",
      "调用 1 个连接器",
    ]);
  });

  it("returns an empty list for no tools", () => {
    expect(summarizeTools([])).toEqual([]);
  });
});

describe("failedTools / failureSummary", () => {
  it("only surfaces failed calls", () => {
    const tools = [tool("Read"), tool("Bash", "error"), tool("Bash", "running")];
    expect(failedTools(tools).map((item) => item.id)).toEqual(["tool-Bash"]);
  });

  it("truncates long failure output to one line", () => {
    const failed = {
      ...tool("Bash", "error"),
      call: {
        ...tool("Bash").call,
        result: `Error: ${"x".repeat(300)}`,
        state: "error",
      },
    };
    const summary = failureSummary(failed);
    expect(summary.length).toBeLessThanOrEqual(161);
    expect(summary.endsWith("…")).toBe(true);
  });

  it("falls back to a generic label without output", () => {
    expect(failureSummary(tool("Bash", "error"))).toBe("执行失败");
  });
});
