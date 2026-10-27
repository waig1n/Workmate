// Dedicated SSE reverse proxy.
//
// Next.js rewrites are convenient for ordinary JSON APIs, but their generic
// proxy path may buffer a fetch response.  Chat tokens need a Route Handler
// that returns the upstream ReadableStream directly and never materializes
// the body.

export const dynamic = "force-dynamic";
export const revalidate = 0;
export const runtime = "nodejs";

export async function GET(
  request: Request,
  context: { params: Promise<{ conversationId: string }> },
): Promise<Response> {
  const { conversationId } = await context.params;
  const api = process.env.PRODUCT_API_URL ?? "http://gateway:8080";
  const headers = new Headers({
    Accept: "text/event-stream",
  });
  const authorization = request.headers.get("authorization");
  const lastEventId = request.headers.get("last-event-id");
  if (authorization) headers.set("Authorization", authorization);
  if (lastEventId) headers.set("Last-Event-ID", lastEventId);

  try {
    const upstream = await fetch(
      `${api}/api/tasks/conversations/${encodeURIComponent(conversationId)}/events`,
      {
        method: "GET",
        headers,
        cache: "no-store",
        signal: request.signal,
      },
    );
    if (!upstream.body) {
      return new Response("SSE upstream returned no body", { status: 502 });
    }
    const retryAfter = upstream.headers.get("Retry-After");
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "Content-Encoding": "identity",
        "X-Accel-Buffering": "no",
        ...(retryAfter !== null ? { "Retry-After": retryAfter } : {}),
      },
    });
  } catch (error) {
    if (request.signal.aborted) {
      return new Response(null, { status: 499 });
    }
    return Response.json(
      {
        detail:
          error instanceof Error ? error.message : "SSE upstream unavailable",
      },
      { status: 502 },
    );
  }
}
