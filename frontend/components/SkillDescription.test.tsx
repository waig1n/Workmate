import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { SkillDescription } from "./SkillDescription";

it("loads full instructions only on expansion and renders markup as text", async () => {
  const get = vi.spyOn(api, "get").mockResolvedValue({ description: "Details", markdown: "<script>alert(1)</script>" });
  const view = render(createElement(QueryClientProvider, { client: new QueryClient() },
    createElement(SkillDescription, { id: "skill-1", summary: "Summary" })));
  expect(get).not.toHaveBeenCalled();
  const details = view.container.querySelector("details")!;
  details.open = true;
  fireEvent(details, new Event("toggle"));
  await waitFor(() => expect(screen.getByText("<script>alert(1)</script>")).toBeTruthy());
  expect(view.container.querySelector("script")).toBeNull();
  expect(get).toHaveBeenCalledWith("/skills/skill-1/description");
  get.mockRestore();
});

it("does not reveal cached instructions while current permission check fails", async () => {
 const get = vi.spyOn(api, "get").mockRejectedValue(new Error("Forbidden"));
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
 client.setQueryData(["skill-description", "revoked"], { description: "缓存私密描述", markdown: "缓存私密全文" });
 const view = render(createElement(QueryClientProvider, { client }, createElement(SkillDescription, { id: "revoked", summary: "公开简介" })));
 expect(screen.queryByText("缓存私密全文")).toBeNull();
 const disclosure = view.container.querySelector("details")!;
 disclosure.open = true; fireEvent(disclosure, new Event("toggle"));
 await screen.findByRole("button", { name: "说明加载失败，点击重试" });
 expect(screen.queryByText("缓存私密全文")).toBeNull();
 expect(screen.queryByText("缓存私密描述")).toBeNull();
 get.mockRestore();
});
