// @vitest-environment node
import { afterEach, expect, it, vi } from "vitest";
import { GET } from "./route";

afterEach(() => vi.unstubAllGlobals());

it("preserves the Gateway retry delay on an unavailable stream", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("unavailable", {
    status: 503,
    headers: { "Retry-After": "7" },
  })));
  const response = await GET(new Request("http://frontend/api/tasks/conversations/c/events"), {
    params: Promise.resolve({ conversationId: "c" }),
  });
  expect(response.status).toBe(503);
  expect(response.headers.get("Retry-After")).toBe("7");
});

it("forwards authorization and cursor while streaming without waiting for EOF", async () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  const fetch = vi.fn().mockResolvedValue(new Response(body));
  vi.stubGlobal("fetch", fetch);
  const response = await GET(new Request("http://frontend/api/tasks/conversations/c/events", {
    headers: { Authorization: "Bearer fixture", "Last-Event-ID": "rt1.fixture" },
  }), { params: Promise.resolve({ conversationId: "c" }) });
  const forwarded = fetch.mock.calls[0][1].headers as Headers;
  expect(forwarded.get("Authorization")).toBe("Bearer fixture");
  expect(forwarded.get("Last-Event-ID")).toBe("rt1.fixture");
  expect(response.headers.get("Retry-After")).toBeNull();
  const reader = response.body!.getReader();
  controller.enqueue(new TextEncoder().encode("data: first\n\n"));
  expect(new TextDecoder().decode((await reader.read()).value)).toBe("data: first\n\n");
  controller.close();
  expect((await reader.read()).done).toBe(true);
});
