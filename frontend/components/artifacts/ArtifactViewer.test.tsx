import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Artifact } from "@/lib/types";
import { ArtifactViewer } from "./ArtifactViewer";

const { fetchArtifactBlob, downloadArtifact } = vi.hoisted(() => ({
  fetchArtifactBlob: vi.fn(),
  downloadArtifact: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ fetchArtifactBlob, downloadArtifact }));

function artifact(name: string, mimeType: string): Artifact {
  return {
    id: "artifact-1",
    task_id: "task-1",
    conversation_id: "conversation-1",
    name,
    mime_type: mimeType,
    size: 12,
    version: 1,
    change_type: "created",
    metadata: {},
    created_at: "2026-08-31T00:00:00Z",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("ArtifactViewer", () => {
  beforeEach(() => {
    fetchArtifactBlob.mockReset();
    downloadArtifact.mockReset();
  });

  it("revokes authenticated image object URLs when unmounted", async () => {
    fetchArtifactBlob.mockResolvedValue(new Blob(["image"], { type: "image/png" }));
    const createUrl = vi.fn(() => "blob:safe-preview");
    const revokeUrl = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createUrl });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeUrl });
    const view = render(createElement(ArtifactViewer, {
      artifact: artifact("chart.png", "image/png"),
    }));

    expect(await screen.findByRole("img", { name: "chart.png" })).toHaveAttribute(
      "src",
      "blob:safe-preview",
    );
    view.unmount();
    expect(createUrl).toHaveBeenCalledOnce();
    expect(revokeUrl).toHaveBeenCalledWith("blob:safe-preview");
  });

  it("previews HTML in a sandbox and switches to the unchanged source", async () => {
    const source = '<h1>Report</h1><script>alert("test")</script>';
    fetchArtifactBlob.mockResolvedValue({ text: async () => source });
    render(createElement(ArtifactViewer, { artifact: artifact("result.html", "text/html") }));
    const frame = await screen.findByTitle("result.html 交互预览");
    expect(frame).toHaveAttribute("sandbox", "allow-scripts");
    expect(frame).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(frame.getAttribute("srcdoc")).toContain('alert("test")');
    fireEvent.click(screen.getByRole("button", { name: "源码" }));
    expect(screen.getByText(source)).toBeInTheDocument();
    expect(screen.queryByTitle("result.html 交互预览")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    expect(screen.getByTitle("result.html 交互预览")).toBeInTheDocument();
  });

  it.each(["download", "oversize"])("clears an in-flight preview when selecting %s content", async (kind) => {
    const request = deferred<Blob>();
    fetchArtifactBlob.mockReturnValue(request.promise);
    const view = render(createElement(ArtifactViewer, { artifact: artifact("old.txt", "text/plain") }));
    expect(screen.getByLabelText("正在加载预览")).toBeInTheDocument();
    const next = kind === "download"
      ? { ...artifact("macro.js", "text/javascript"), id: "next" }
      : { ...artifact("large.txt", "text/plain"), id: "next", size: 6 * 1024 * 1024 };
    view.rerender(createElement(ArtifactViewer, { artifact: next }));
    expect(screen.queryByLabelText("正在加载预览")).not.toBeInTheDocument();
    expect(screen.getByText(kind === "download"
      ? "此类型仅支持安全下载" : "文件超过 5 MB 前端预览上限，请下载后查看")).toBeInTheDocument();
    expect(fetchArtifactBlob).toHaveBeenCalledTimes(1);
    // Resolve the old fetch anyway; cleanup cannot rely on transport honoring abort.
    await act(async () => { request.resolve({ text: async () => "old content" } as Blob); });
    expect(screen.queryByText("old content")).not.toBeInTheDocument();
  });

  it("a late blob.text result cannot overwrite the newly selected version", async () => {
    const oldText = deferred<string>();
    const readOldText = vi.fn(() => oldText.promise);
    const newRequest = deferred<Blob>();
    fetchArtifactBlob.mockResolvedValueOnce({ text: readOldText }).mockReturnValueOnce(newRequest.promise);
    const first = artifact("report.txt", "text/plain");
    const view = render(createElement(ArtifactViewer, { artifact: first }));
    await waitFor(() => expect(readOldText).toHaveBeenCalledOnce());
    view.rerender(createElement(ArtifactViewer, { artifact: { ...first, id: "artifact-2", version: 2 } }));
    await act(async () => { newRequest.resolve({ text: async () => "version two content" } as Blob); });
    expect(await screen.findByText("version two content")).toBeInTheDocument();
    await act(async () => { oldText.resolve("obsolete version one content"); });
    expect(screen.queryByText("obsolete version one content")).not.toBeInTheDocument();
    expect(screen.getByText("version two content")).toBeInTheDocument();
  });

  it("shows a current preview failure and downloads the selected immutable version", async () => {
    fetchArtifactBlob.mockRejectedValue(new Error("成果文件已不存在"));
    downloadArtifact.mockResolvedValue(undefined);
    const selected = { ...artifact("report.txt", "text/plain"), id: "version-two", version: 2 };
    render(createElement(ArtifactViewer, { artifact: selected }));
    expect(await screen.findByText("成果文件已不存在")).toBeInTheDocument();
    expect(screen.queryByLabelText("正在加载预览")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下载文件" }));
    expect(downloadArtifact).toHaveBeenCalledWith("version-two", "report.txt");
  });
});

it("checks the actual HTML blob size before decoding", async () => {
  const text = vi.fn(async () => "oversized HTML");
  fetchArtifactBlob.mockResolvedValue({ size: 6 * 1024 * 1024, text });
  render(createElement(ArtifactViewer, { artifact: artifact("report.xhtml", "application/xhtml+xml") }));
  expect(await screen.findByText("文件超过 5 MB 前端预览上限，请下载后查看")).toBeInTheDocument();
  expect(text).not.toHaveBeenCalled();
  expect(screen.queryByTitle("report.xhtml 交互预览")).not.toBeInTheDocument();
});

it("keeps original source accessible when a local chart library fails", async () => {
  const source = '<div id="plot"></div><script>echarts.init(document.getElementById("plot"))</script>';
  fetchArtifactBlob.mockResolvedValue({ text: async () => source });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
  try {
    render(createElement(ArtifactViewer, { artifact: artifact("report.html", "text/html") }));
    await screen.findByText("图表库加载失败，请重试或下载文件");
    fireEvent.click(screen.getByRole("button", { name: "源码" }));
    expect(screen.getByText(source)).toBeInTheDocument();
    expect(screen.queryByTitle("report.html 交互预览")).not.toBeInTheDocument();
  } finally { vi.unstubAllGlobals(); }
});

it("discards a late chart library response after selecting another HTML file", async () => {
  const oldLibrary = deferred<{ ok: boolean; text: () => Promise<string> }>();
  fetchArtifactBlob.mockResolvedValueOnce({ text: async () => '<script>echarts.init(x)</script>' })
    .mockResolvedValueOnce({ text: async () => '<h1>New report</h1>' });
  const load = vi.fn(() => oldLibrary.promise);
  vi.stubGlobal("fetch", load);
  try {
    const first = artifact("old.html", "text/html");
    const view = render(createElement(ArtifactViewer, { artifact: first }));
    await waitFor(() => expect(load).toHaveBeenCalledOnce());
    view.rerender(createElement(ArtifactViewer, { artifact: { ...first, id: "new", name: "new.html" } }));
    expect(await screen.findByTitle("new.html 交互预览")).toHaveAttribute("srcdoc", expect.stringContaining("New report"));
    await act(async () => oldLibrary.resolve({ ok: true, text: async () => "window.echarts={}" }));
    expect(screen.getByTitle("new.html 交互预览")).toHaveAttribute("srcdoc", expect.stringContaining("New report"));
    expect(screen.getByTitle("new.html 交互预览").getAttribute("srcdoc")).not.toContain("echarts.init");
  } finally { vi.unstubAllGlobals(); }
});
