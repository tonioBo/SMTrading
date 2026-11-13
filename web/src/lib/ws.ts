/**
 * Live WebSocket `/ws` (one connection for the whole app): typed emitter for `event`
 * and `quote` messages, exponential reconnect 1s → 30s, quote subscriptions with
 * reference counting re-sent on every reconnect. Close 4401 (session gone) does not
 * reconnect: the app goes to /login.
 */
import { useEffect, useRef, useSyncExternalStore } from "react";
import { notifyUnauthorized } from "@/api/client";

export interface LiveEvent {
  type: string;
  ts?: string;
  [key: string]: unknown;
}

export interface LiveQuote {
  symbol: string;
  [key: string]: unknown;
}

export type SocketStatus = "idle" | "connecting" | "open" | "closed";

interface Messages {
  event: LiveEvent;
  quote: LiveQuote;
}

type Listener<T> = (payload: T) => void;

export const RECONNECT_MIN_MS = 1000;
export const RECONNECT_MAX_MS = 30000;
export const CLOSE_UNAUTHORIZED = 4401;
const WS_OPEN = 1;

export function reconnectDelay(attempt: number): number {
  return Math.min(RECONNECT_MIN_MS * 2 ** attempt, RECONNECT_MAX_MS);
}

function defaultUrl(): string {
  const { protocol, host } = window.location;
  return `${protocol === "https:" ? "wss" : "ws"}://${host}/ws`;
}

export interface LiveSocketOptions {
  url?: () => string;
  createSocket?: (url: string) => WebSocket;
}

export class LiveSocket {
  status: SocketStatus = "idle";
  private ws: WebSocket | null = null;
  private users = 0;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly symbols = new Map<string, number>();
  private readonly listeners: { [K in keyof Messages]: Set<Listener<Messages[K]>> } = {
    event: new Set(),
    quote: new Set(),
  };
  private readonly statusListeners = new Set<() => void>();
  private readonly url: () => string;
  private readonly createSocket: (url: string) => WebSocket;

  constructor(options: LiveSocketOptions = {}) {
    this.url = options.url ?? defaultUrl;
    this.createSocket = options.createSocket ?? ((url) => new WebSocket(url));
  }

  on<K extends keyof Messages>(type: K, listener: Listener<Messages[K]>): () => void {
    const set = this.listeners[type] as Set<Listener<Messages[K]>>;
    set.add(listener);
    return () => set.delete(listener);
  }

  onStatus(listener: () => void): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  /** Keeps the socket connected until the returned release is called by every user. */
  acquire(): () => void {
    this.users += 1;
    if (this.users === 1) this.connect();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      this.users -= 1;
      if (this.users === 0) this.disconnect();
    };
  }

  /** Subscribe to quotes of `symbols`; the returned function unsubscribes. */
  subscribeQuotes(symbols: string[]): () => void {
    const added = symbols.filter((s) => {
      const n = this.symbols.get(s) ?? 0;
      this.symbols.set(s, n + 1);
      return n === 0;
    });
    if (added.length) this.send({ type: "subscribe_quotes", symbols: added });
    return () => {
      const removed = symbols.filter((s) => {
        const n = (this.symbols.get(s) ?? 1) - 1;
        if (n <= 0) this.symbols.delete(s);
        else this.symbols.set(s, n);
        return n <= 0;
      });
      if (removed.length) this.send({ type: "unsubscribe_quotes", symbols: removed });
    };
  }

  private setStatus(status: SocketStatus): void {
    this.status = status;
    this.statusListeners.forEach((l) => l());
  }

  private send(message: object): void {
    if (this.ws?.readyState === WS_OPEN) this.ws.send(JSON.stringify(message));
  }

  private connect(): void {
    this.clearTimer();
    this.setStatus("connecting");
    const ws = this.createSocket(this.url());
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.setStatus("open");
      if (this.symbols.size) this.send({ type: "subscribe_quotes", symbols: [...this.symbols.keys()] });
    };
    ws.onmessage = (e: MessageEvent) => this.dispatch(e.data);
    ws.onclose = (e: CloseEvent) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.setStatus("closed");
      if (e.code === CLOSE_UNAUTHORIZED) {
        notifyUnauthorized();
        return;
      }
      if (this.users > 0) this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    const delay = reconnectDelay(this.attempt);
    this.attempt += 1;
    this.timer = setTimeout(() => {
      this.timer = null;
      if (this.users > 0) this.connect();
    }, delay);
  }

  private disconnect(): void {
    this.clearTimer();
    this.attempt = 0;
    const ws = this.ws;
    this.ws = null;
    ws?.close();
    this.setStatus("idle");
  }

  private clearTimer(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  private dispatch(data: unknown): void {
    let msg: { type?: string; event?: LiveEvent; quote?: LiveQuote };
    try {
      msg = JSON.parse(String(data));
    } catch {
      return;
    }
    if (msg.type === "event" && msg.event) this.listeners.event.forEach((l) => l(msg.event!));
    else if (msg.type === "quote" && msg.quote) this.listeners.quote.forEach((l) => l(msg.quote!));
  }
}

export const liveSocket = new LiveSocket();

/** Keeps the shared socket connected while mounted; returns it with its status. */
export function useLiveSocket(socket: LiveSocket = liveSocket): { socket: LiveSocket; status: SocketStatus } {
  const status = useSyncExternalStore(
    (cb) => socket.onStatus(cb),
    () => socket.status,
  );
  useEffect(() => socket.acquire(), [socket]);
  return { socket, status };
}

/** Calls `handler` for each `type` message while mounted (latest handler, no resubscribe). */
export function useLiveMessage<K extends keyof Messages>(
  type: K,
  handler: (payload: Messages[K]) => void,
  socket: LiveSocket = liveSocket,
): void {
  const ref = useRef(handler);
  useEffect(() => {
    ref.current = handler;
  });
  useEffect(() => socket.on(type, (p) => ref.current(p)), [socket, type]);
}
