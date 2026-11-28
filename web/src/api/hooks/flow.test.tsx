import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import type { FlowEvent } from "@/api/types";
import { createQueryClient } from "@/queryClient";
import { useFlowEvents } from "./flow";

const ev = (id: string, ts: string): FlowEvent => ({
  id,
  ts,
  kind: "mcp",
  who: "agent:a1",
  path: ["agent:a1", "pc", "mcp", "db"],
  hops: ["list_orders", "list_orders", "list_orders"],
  outcome: "ok",
  latency_ms: 40,
  symbol: null,
});

const asked: (string | null)[] = [];
let replies: (() => Response)[] = [];

const server = setupServer(
  http.get("/api/flow/events", ({ request }) => {
    asked.push(new URL(request.url).searchParams.get("after"));
    return (replies.shift() ?? (() => HttpResponse.json({ events: [], cursor: "x", now: "x" })))();
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  asked.length = 0;
});
afterAll(() => server.close());

describe("useFlowEvents", () => {
  it("asks after the cursor, drops the ids already seen and recovers after a failed poll", async () => {
    const a = ev("mcp:1", "2026-11-28T10:00:00Z");
    const b = ev("fill:9", "2026-11-28T10:00:05Z");
    replies = [
      () => HttpResponse.json({ events: [a], cursor: a.ts, now: "2026-11-28T10:00:01Z" }),
      () => HttpResponse.json({ code: "HTTP_ERROR", message: "boom" }, { status: 503 }),
      // the 10 s overlap re-reads «a»: handed over once only
      () => HttpResponse.json({ events: [a, b], cursor: b.ts, now: "2026-11-28T10:00:06Z" }),
    ];
    const client = createQueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result } = renderHook(() => useFlowEvents(), { wrapper });
    const poll = () => act(() => client.refetchQueries({ queryKey: ["flow", "events"] }));

    await waitFor(() => expect(result.current.events.map((e) => e.id)).toEqual(["mcp:1"]));
    expect(result.current.stale).toBe(false);

    await poll();
    await waitFor(() => expect(result.current.stale).toBe(true));
    expect(result.current.events.map((e) => e.id)).toEqual(["mcp:1"]); // the last batch is kept

    await poll();
    await waitFor(() => expect(result.current.stale).toBe(false));
    expect(result.current.events.map((e) => e.id)).toEqual(["fill:9"]);
    expect(result.current.now).toBe("2026-11-28T10:00:06Z");
    expect(asked).toEqual([null, a.ts, a.ts]); // the failed poll did not move the cursor
  });
});
