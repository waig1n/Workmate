import { render, screen } from "@testing-library/react";
import { createElement } from "react";
import { describe, expect, it } from "vitest";

import { MarkdownMessage } from "./MarkdownMessage";

describe("MarkdownMessage", () => {
  it("renders GFM content and keeps raw HTML inert", () => {
    const { container } = render(createElement(
      MarkdownMessage,
      {
        content: [
          "# 报告",
          "",
          "| 项目 | 状态 |",
          "| --- | --- |",
          "| P0 | 完成 |",
          "",
          "<script>window.pwned = true</script>",
        ].join("\n"),
      },
    ));

    expect(screen.getByRole("heading", { name: "报告" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
  });

  it("blocks unsafe link protocols", () => {
    render(createElement(MarkdownMessage, {
      content: "[危险链接](javascript:alert(1))",
    }));
    expect(screen.getByText("危险链接").closest("a")).not.toHaveAttribute(
      "href",
      expect.stringContaining("javascript:"),
    );
  });

  it("keeps a 100 KB class response renderable", () => {
    const content = `# 长报告\n\n${"正文内容 ".repeat(20_000)}`;
    render(createElement(MarkdownMessage, { content }));
    expect(screen.getByRole("heading", { name: "长报告" })).toBeInTheDocument();
  });
});
