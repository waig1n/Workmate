import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Artifact } from "@/lib/types";
import { downloadArtifact } from "@/lib/api";
vi.mock("@/lib/api", () => ({ downloadArtifact: vi.fn() }));
import { ArtifactReferences } from "./ArtifactReferences";

function artifact(id: string, version: number): Artifact {
  return {
    id,
    task_id: "task-1",
    conversation_id: "conversation-1",
    name: "report.md",
    mime_type: "text/markdown",
    size: 12,
    version,
    change_type: "modified",
    metadata: {},
    created_at: `2026-09-0${version}T00:00:00Z`,
  };
}

describe("ArtifactReferences", () => {
  it("references the latest revision and opens that exact artifact", () => {
    const onOpen = vi.fn();
    render(<ArtifactReferences artifacts={[artifact("v1", 1), artifact("v2", 2)]} onOpen={onOpen} />);

    expect(screen.getByText(/v2/)).toBeVisible();
    expect(screen.queryByText(/v1/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "打开成果 report.md" }));
    expect(onOpen).toHaveBeenCalledWith("v2");
  });
});

it("downloads independently, hides the first version and reports failures", async () => {
  const onOpen = vi.fn();
  vi.mocked(downloadArtifact).mockRejectedValueOnce(new Error("网络错误"));
  render(<ArtifactReferences artifacts={[artifact("v1", 1)]} onOpen={onOpen} />);
  expect(screen.queryByText(/v1/)).toBeNull();
  expect(screen.getByText(/Markdown · 12 B/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "下载 report.md" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("网络错误"));
  expect(downloadArtifact).toHaveBeenCalledWith("v1", "report.md");
  expect(onOpen).not.toHaveBeenCalled();
  vi.mocked(downloadArtifact).mockResolvedValueOnce(undefined);
  fireEvent.click(screen.getByRole("button", { name: "下载 report.md" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
});

it("lists multiple file types with readable sizes and preserves a long file name", () => {
  const longName = "很长的演示文稿标题".repeat(10) + ".pptx";
  render(<ArtifactReferences artifacts={[
    { ...artifact("ppt", 1), name: longName, size: 131072 },
    { ...artifact("pdf", 1), name: "报告.pdf", size: 2097152 },
  ]} onOpen={vi.fn()} />);
  expect(screen.getByText("PowerPoint · 128 KB")).toBeVisible();
  expect(screen.getByText("PDF · 2.0 MB")).toBeVisible();
  expect(screen.getByTitle(longName)).toHaveTextContent(longName);
  expect(screen.getAllByRole("button", { name: /^下载/ })).toHaveLength(2);
});
