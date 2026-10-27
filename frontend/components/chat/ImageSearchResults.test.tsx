import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { ImageSearchResults } from "./ImageSearchResults";

it("shows only exact final references, ordered and deduplicated across searches", () => {
  const result = JSON.stringify({ category: "images", results: [
    { title: "未选中", url: "https://example.com/a", image_url: "https://example.com/a.jpg" },
    { title: "选中", url: "https://example.com/b", image_url: "https://example.com/b.jpg" },
  ] });
  const { rerender } = render(<ImageSearchResults result={[result, result]} answer="推荐 [这张](https://example.com/b)。" />);
  expect(screen.getByText("选中")).toBeTruthy();
  expect(screen.queryByText("未选中")).toBeNull();
  expect(screen.getAllByRole("link", { name: "查看原图" })).toHaveLength(1);
  rerender(<ImageSearchResults result={result} answer="https://example.com/b-other" />);
  expect(screen.queryByRole("region")).toBeNull();
});

it("does not infer selection from an ambiguous source page or absent references", () => {
  const result = JSON.stringify({ category: "images", results: [
    { url: "https://example.com/page", image_url: "https://example.com/1.jpg" },
    { url: "https://example.com/page", image_url: "https://example.com/2.jpg" },
  ] });
  const { rerender } = render(<ImageSearchResults result={result} answer="https://example.com/page" />);
  expect(screen.queryByRole("region")).toBeNull();
  rerender(<ImageSearchResults result={result} answer="https://example.com/2.jpg" />);
  expect(screen.getAllByRole("link", { name: "查看原图" })).toHaveLength(1);
});

it("loads external thumbnails only on request and handles image errors", () => {
  render(<ImageSearchResults answer="推荐 https://example.com/page" result={JSON.stringify({ category: "images", results: [{
    title: "雪山", url: "https://example.com/page", image_url: "https://example.com/full.jpg",
    thumbnail_url: "https://example.com/thumb.jpg",
  }] })} />);
  expect(screen.queryByRole("img")).toBeNull();
  expect(screen.getByRole("link", { name: "来源页" }).getAttribute("href")).toBe("https://example.com/page");
  fireEvent.click(screen.getByRole("button", { name: /加载缩略图/ }));
  const image = screen.getByRole("img");
  expect(image.getAttribute("referrerpolicy")).toBe("no-referrer");
  fireEvent.error(image);
  expect(screen.getByText("缩略图不可用")).toBeTruthy();
});

it("ignores partial JSON, unsafe links and ordinary search results", () => {
  const { container, rerender } = render(<ImageSearchResults result="{" />);
  expect(container.textContent).toBe("");
  rerender(<ImageSearchResults result={JSON.stringify({ category: "images", results: [
    { url: "https://example.com", image_url: "javascript:alert(1)" },
    { url: "https://example.com", image_url: "http://127.0.0.1/admin" },
  ] })} />);
  expect(container.textContent).toBe("");
});
