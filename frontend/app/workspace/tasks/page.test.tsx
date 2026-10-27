import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import TasksPage from "./page";

const navigation = vi.hoisted(() => ({ query: "" }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(navigation.query) }));
beforeEach(() => { navigation.query = ""; });
afterEach(() => vi.restoreAllMocks());
const item = (id: string) => ({ id, title: `对话 ${id}`, updated_at: "2026-09-11T10:00:00Z", agent: { display_name: "助手" } });
function Wrapper({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

it("loads conversation pages and searches the server, including empty conversations", async () => {
  const get = vi.spyOn(api, "get").mockImplementation(async (path) => {
    if (path.includes("q=")) return { items: [], next_cursor: null };
    if (path.includes("cursor=")) return { items: [item("2")], next_cursor: null };
    return { items: [item("1")], next_cursor: "next-page" };
  });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><TasksPage /></QueryClientProvider>);
  expect(await screen.findByText("对话 1")).toBeTruthy();
  const create = vi.fn();
  window.addEventListener("workmate:create", create);
  fireEvent.click(screen.getByRole("button", { name: "新建对话" }));
  expect(create).toHaveBeenCalledOnce();
  window.removeEventListener("workmate:create", create);
  expect(screen.getByRole("link", { name: /对话 1/ }).getAttribute("href")).toBe("/workspace/c/1");
  fireEvent.click(screen.getByRole("button", { name: "加载更多对话" }));
  expect(await screen.findByText("对话 2")).toBeTruthy();
  expect(screen.getByText("对话 1")).toBeTruthy();
  expect(screen.getByText("已显示全部对话")).toBeTruthy();
  expect(get).toHaveBeenCalledWith("/conversations/history?limit=20&cursor=next-page");
  fireEvent.change(screen.getByRole("textbox", { name: "搜索历史对话" }), { target: { value: "报告" } });
  await waitFor(() => expect(screen.getByText("没有找到匹配的对话，试试其他关键词。")).toBeTruthy());
  expect(get).toHaveBeenCalledWith("/conversations/history?limit=20&q=%E6%8A%A5%E5%91%8A");
});


it("keeps the first page visible when loading more fails and retries the failed page", async () => {
  let attempts = 0;
  const get = vi.spyOn(api, "get").mockImplementation(async (path) => {
    if (path.includes("cursor=")) {
      if (attempts++ === 0) throw new Error("offline");
      return { items: [item("2")], next_cursor: null };
    }
    return { items: [item("1")], next_cursor: "next" };
  });
  render(<TasksPage />, { wrapper: Wrapper });
  await screen.findByText("对话 1");
  fireEvent.click(screen.getByRole("button", { name: "加载更多对话" }));
  await screen.findByRole("alert");
  expect(screen.getByText("对话 1")).toBeTruthy();
  expect(screen.queryByText("还没有历史对话，开始一段新对话吧。")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByText("对话 2");
  expect(screen.getByText("对话 1")).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(get.mock.calls.filter(([path]) => !path.includes("cursor=")).length).toBe(1);
});

it("retries initial loading failures", async () => {
  vi.spyOn(api, "get").mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValue({ items: [item("1")], next_cursor: null });
  render(<TasksPage />, { wrapper: Wrapper });
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByText("对话 1");
  expect(screen.queryByRole("alert")).toBeNull();
});

it("updates search when navigation changes q without remounting", async () => {
  navigation.query = "q=first";
  const get = vi.spyOn(api, "get").mockResolvedValue({ items: [], next_cursor: null });
  const view = render(<TasksPage />, { wrapper: Wrapper });
  await waitFor(() => expect(get).toHaveBeenCalledWith("/conversations/history?limit=20&q=first"));
  navigation.query = "q=second";
  view.rerender(<TasksPage />);
  await waitFor(() => expect(get).toHaveBeenCalledWith("/conversations/history?limit=20&q=second"));
  expect((screen.getByRole("textbox", { name: "搜索历史对话" }) as HTMLInputElement).value).toBe("second");
});
