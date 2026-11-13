import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { configureClient, resetUnauthorized } from "@/api/client";
import { LiveSocket, reconnectDelay } from "./ws";

class FakeSocket {
  static instances: FakeSocket[] = [];
  readyState = 0;
  sent: unknown[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  drop(code = 1006) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  receive(msg: unknown) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
}

const make = () =>
  new LiveSocket({
    url: () => "ws://test/ws",
    createSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
  });
const last = () => FakeSocket.instances[FakeSocket.instances.length - 1];

beforeEach(() => {
  FakeSocket.instances = [];
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

describe("reconnectDelay", () => {
  it("doubles from 1s and caps at 30s", () => {
    expect([0, 1, 2, 3, 4, 5, 6, 10].map(reconnectDelay)).toEqual([
      1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000,
    ]);
  });
});

describe("LiveSocket", () => {
  it("dispatches typed event and quote messages, ignores ping", () => {
    const s = make();
    const events = vi.fn();
    const quotes = vi.fn();
    s.on("event", events);
    s.on("quote", quotes);
    s.acquire();
    last().open();
    last().receive({ type: "ping" });
    last().receive({ type: "event", event: { type: "kill_switch", on: true } });
    last().receive({ type: "quote", quote: { symbol: "AAPL", last: "1.5" } });
    expect(events).toHaveBeenCalledWith({ type: "kill_switch", on: true });
    expect(quotes).toHaveBeenCalledWith({ symbol: "AAPL", last: "1.5" });
  });

  it("reconnects with exponential backoff and re-subscribes quotes", () => {
    const s = make();
    s.acquire();
    s.subscribeQuotes(["AAPL", "MSFT"]);
    last().open();
    expect(last().sent).toEqual([{ type: "subscribe_quotes", symbols: ["AAPL", "MSFT"] }]);

    last().drop();
    vi.advanceTimersByTime(999);
    expect(FakeSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(2);
    last().drop();
    vi.advanceTimersByTime(1999);
    expect(FakeSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(3);
    last().open();
    expect(last().sent).toEqual([{ type: "subscribe_quotes", symbols: ["AAPL", "MSFT"] }]);
    // after a successful open the backoff restarts from 1s
    last().drop();
    vi.advanceTimersByTime(1000);
    expect(FakeSocket.instances).toHaveLength(4);
  });

  it("reference-counts quote subscriptions", () => {
    const s = make();
    s.acquire();
    last().open();
    const a = s.subscribeQuotes(["AAPL"]);
    const b = s.subscribeQuotes(["AAPL", "MSFT"]);
    a();
    b();
    expect(last().sent).toEqual([
      { type: "subscribe_quotes", symbols: ["AAPL"] },
      { type: "subscribe_quotes", symbols: ["MSFT"] },
      { type: "unsubscribe_quotes", symbols: ["AAPL", "MSFT"] },
    ]);
  });

  it("stops reconnecting when the last user releases", () => {
    const s = make();
    const release = s.acquire();
    last().open();
    release();
    expect(s.status).toBe("idle");
    vi.advanceTimersByTime(60000);
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("on close 4401 goes to login and does not reconnect", () => {
    const onUnauthorized = vi.fn();
    configureClient({ onUnauthorized });
    resetUnauthorized();
    window.history.replaceState(null, "", "/");
    const s = make();
    s.acquire();
    last().open();
    last().drop(4401);
    vi.advanceTimersByTime(60000);
    expect(FakeSocket.instances).toHaveLength(1);
    expect(onUnauthorized).toHaveBeenCalledWith("/login?next=%2F");
  });
});
