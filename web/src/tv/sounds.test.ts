import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TvSound, TvSoundEvent } from "@/api/tvTypes";
import { MIN_GAP_MS, TONES, TvSounds } from "./sounds";

/** a stand-in AudioContext: records each tone (frequency, start) and the master volume */
class FakeContext {
  state: "suspended" | "running" | "closed" = "running";
  currentTime = 0;
  destination = {};
  tones: { freq: number; at: number }[] = [];
  gains: number[] = [];
  closed = false;
  private listeners: (() => void)[] = [];
  addEventListener(_t: string, f: () => void) {
    this.listeners.push(f);
  }
  removeEventListener() {}
  resume() {
    this.state = "running";
    this.listeners.forEach((f) => f());
    return Promise.resolve();
  }
  close() {
    this.closed = true;
    this.state = "closed";
    return Promise.resolve();
  }
  createGain() {
    const param = { value: 1, setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() };
    const node = { gain: param, connect: (n: unknown) => n, disconnect: vi.fn() };
    this.gains.push(0);
    const idx = this.gains.length - 1;
    Object.defineProperty(param, "value", {
      get: () => this.gains[idx],
      set: (v: number) => {
        this.gains[idx] = v;
      },
    });
    return node;
  }
  createOscillator() {
    const osc = {
      type: "sine",
      frequency: { value: 0, setValueAtTime: (v: number) => (osc.frequency.value = v) },
      connect: (n: unknown) => n,
      disconnect: vi.fn(),
      start: (at: number) => this.tones.push({ freq: osc.frequency.value, at }),
      stop: vi.fn(),
      onended: null as null | (() => void),
    };
    return osc;
  }
}

const ON: TvSound = { enabled: true, volume: 40, quiet_start: "22:00", quiet_end: "08:00", tz: "Europe/Rome", quiet_now: false };
const ev = (id: string, kind: TvSoundEvent["kind"]): TvSoundEvent => ({ id, kind, at: "2026-11-28T10:00:00Z" });

let ctx: FakeContext;
let sounds: TvSounds;
const make = () => {
  ctx = new FakeContext();
  sounds = new TvSounds({ createContext: () => ctx as unknown as AudioContext });
};

beforeEach(() => {
  vi.useFakeTimers();
  make();
});
afterEach(() => {
  sounds.destroy();
  vi.useRealTimers();
});

const freqs = () => ctx.tones.map((t) => t.freq);

describe("TvSounds", () => {
  it("stays silent and creates no audio while sounds are off (the default)", () => {
    let created = 0;
    sounds = new TvSounds({ createContext: () => (created++, ctx as unknown as AudioContext) });
    sounds.configure({ ...ON, enabled: false });
    sounds.handle([ev("fill:1", "fill_buy")]);
    expect(created).toBe(0);
    expect(ctx.tones).toEqual([]);
  });

  it("plays a short chime for a fill, a different one for buy and sell", () => {
    sounds.configure(ON);
    sounds.handle([ev("fill:1", "fill_buy")]);
    expect(freqs()).toEqual(TONES.fill_buy.map((t) => t.freq));
    vi.advanceTimersByTime(MIN_GAP_MS);
    sounds.handle([ev("fill:2", "fill_sell")]);
    expect(freqs().slice(TONES.fill_buy.length)).toEqual(TONES.fill_sell.map((t) => t.freq));
    expect(TONES.fill_buy[0].freq).not.toBe(TONES.fill_sell[0].freq);
  });

  it("two notes: descending for a stop, ascending for a target", () => {
    const [s1, s2] = TONES.stop;
    const [t1, t2] = TONES.target;
    expect(s1.freq).toBeGreaterThan(s2.freq);
    expect(t1.freq).toBeLessThan(t2.freq);
  });

  it("plays each event id once", () => {
    sounds.configure(ON);
    sounds.handle([ev("fill:1", "fill_buy")]);
    vi.advanceTimersByTime(MIN_GAP_MS * 2);
    sounds.handle([ev("fill:1", "fill_buy")]);
    vi.advanceTimersByTime(MIN_GAP_MS * 2);
    expect(ctx.tones).toHaveLength(TONES.fill_buy.length);
  });

  it("the first look is a baseline: what it holds never plays", () => {
    sounds.configure(ON);
    sounds.baseline([ev("fill:1", "fill_buy")]);
    sounds.handle([ev("fill:1", "fill_buy")]);
    expect(ctx.tones).toEqual([]);
  });

  it("at most one sound every few seconds: the extra ones merge into one, the most important", () => {
    sounds.configure(ON);
    sounds.handle([ev("fill:1", "fill_buy")]);
    const first = ctx.tones.length;
    sounds.handle([ev("fill:2", "fill_sell"), ev("fill:3", "stop")]);
    sounds.handle([ev("health:x:1:ok", "health_ok")]);
    expect(ctx.tones).toHaveLength(first); // waiting for the gap
    vi.advanceTimersByTime(MIN_GAP_MS);
    expect(freqs().slice(first)).toEqual(TONES.stop.map((t) => t.freq)); // the stop wins
    vi.advanceTimersByTime(MIN_GAP_MS * 3);
    expect(ctx.tones).toHaveLength(first + TONES.stop.length); // nothing else queued
  });

  it("a red health beats everything in the merge", () => {
    sounds.configure(ON);
    sounds.handle([ev("fill:1", "stop"), ev("health:b:1:bad", "health_bad"), ev("fill:2", "target")]);
    expect(freqs()).toEqual(TONES.health_bad.map((t) => t.freq));
  });

  it("nothing during the quiet hours", () => {
    sounds.configure({ ...ON, quiet_now: true });
    sounds.handle([ev("fill:1", "fill_buy")]);
    vi.advanceTimersByTime(MIN_GAP_MS);
    expect(ctx.tones).toEqual([]);
  });

  it("the volume sets the master gain (moderate at 100)", () => {
    sounds.configure({ ...ON, volume: 100 });
    sounds.handle([ev("fill:1", "fill_buy")]);
    const master = ctx.gains[0];
    expect(master).toBeGreaterThan(0);
    expect(master).toBeLessThanOrEqual(0.6);
    sounds.configure({ ...ON, volume: 0 });
    expect(ctx.gains[0]).toBe(0);
  });

  it("blocked by the browser: reports it, drops the events, and unlocks on a key", async () => {
    ctx = new FakeContext();
    ctx.state = "suspended";
    sounds = new TvSounds({ createContext: () => ctx as unknown as AudioContext });
    const changes: boolean[] = [];
    sounds.onChange(() => changes.push(sounds.blocked()));
    sounds.configure(ON);
    expect(sounds.blocked()).toBe(true);
    sounds.handle([ev("fill:1", "fill_buy")]);
    vi.advanceTimersByTime(MIN_GAP_MS);
    expect(ctx.tones).toEqual([]); // not saved for later: no burst when unlocked
    sounds.unlock();
    await Promise.resolve();
    expect(sounds.blocked()).toBe(false);
    expect(changes).toContain(false);
    sounds.handle([ev("fill:2", "fill_sell")]);
    expect(freqs()).toEqual(TONES.fill_sell.map((t) => t.freq));
  });

  it("not blocked while sounds are off", () => {
    ctx.state = "suspended";
    sounds.configure({ ...ON, enabled: false });
    expect(sounds.blocked()).toBe(false);
  });

  it("turning sounds off closes the audio and drops a merged sound waiting", () => {
    sounds.configure(ON);
    sounds.handle([ev("fill:1", "fill_buy")]);
    sounds.handle([ev("fill:2", "stop")]);
    sounds.configure({ ...ON, enabled: false });
    vi.advanceTimersByTime(MIN_GAP_MS * 2);
    expect(ctx.tones).toHaveLength(TONES.fill_buy.length);
    expect(ctx.closed).toBe(true);
  });

  it("remembers a bounded number of ids (days on the TV)", () => {
    sounds.configure({ ...ON, quiet_now: true });
    for (let i = 0; i < 5_000; i++) sounds.handle([ev(`fill:${i}`, "fill_buy")]);
    expect(sounds.seenCount()).toBeLessThanOrEqual(1_000);
  });

  it("without Web Audio in the browser it does nothing and is not «blocked»", () => {
    sounds = new TvSounds({ createContext: () => null });
    sounds.configure(ON);
    sounds.handle([ev("fill:1", "fill_buy")]);
    expect(sounds.blocked()).toBe(false);
  });
});
