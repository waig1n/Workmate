import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Artifact } from "@/lib/types";
import { WorkbenchSidecar } from "./WorkbenchSidecar";

vi.mock("@/components/artifacts/ArtifactViewer", () => ({
  ArtifactViewer: ({ artifact, versions, onVersionChange }: {
    artifact: Artifact | null;
    versions?: Artifact[];
    onVersionChange?: (id: string) => void;
  }) => <div><span data-testid="selected-artifact">{artifact?.id ?? "none"}</span>{versions?.map((item) => <button key={item.id} onClick={() => onVersionChange?.(item.id)}>选择 {item.id}</button>)}</div>,
}));

function artifact(id: string, version: number, name = "report.md"): Artifact {
  return {
    id,
    task_id: `task-${version}`,
    conversation_id: "conversation-1",
    name,
    mime_type: "text/markdown",
    size: 12,
    version,
    change_type: version === 1 ? "created" : "modified",
    metadata: {},
    created_at: `2026-09-0${version}T00:00:00Z`,
  };
}

describe("WorkbenchSidecar", () => {
  it("anchors the desktop resize handle to the sidecar", () => {
    render(<WorkbenchSidecar artifacts={[]} width={460} onResize={vi.fn()} onResetWidth={vi.fn()} onClose={vi.fn()} />);

    const handle = screen.getByRole("separator", { name: "调整成果栏宽度" });
    expect(handle.closest("aside")).toHaveClass("xl:relative");
    expect(handle.closest("aside")).not.toHaveClass("xl:static");
  });

  it("groups revisions, follows latest, and preserves an explicit old selection", () => {
    render(<WorkbenchSidecar artifacts={[artifact("v1", 1), artifact("v2", 2)]} width={460} onResize={vi.fn()} onResetWidth={vi.fn()} onClose={vi.fn()} />);

    expect(screen.getByTestId("selected-artifact")).toHaveTextContent("v2");
    fireEvent.click(screen.getByRole("button", { name: "选择 v1" }));
    expect(screen.getByTestId("selected-artifact")).toHaveTextContent("v1");
    expect(screen.getByText(/已有最新版本 v2/)).toBeInTheDocument();
  });

  it("focuses the artifact selected from the conversation reference", () => {
    render(<WorkbenchSidecar artifacts={[artifact("report", 1), artifact("slides", 1, "slides.pptx")]} focusArtifactId="slides" width={460} onResize={vi.fn()} onResetWidth={vi.fn()} onClose={vi.fn()} />);

    expect(screen.getByTestId("selected-artifact")).toHaveTextContent("slides");
    fireEvent.click(screen.getByRole("button", { name: /report\.md/ }));
    expect(screen.getByTestId("selected-artifact")).toHaveTextContent("report");
  });
});
