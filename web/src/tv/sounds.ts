/**
 * The TV's few sounds (spec «Suoni»): synthesised with Web Audio (no file to download),
 * only for real events the server reports (fills, stop/target, health red/back to ok).
 * Off by default; nothing during the quiet hours (the server says `quiet_now`); at most one
 * sound every MIN_GAP_MS, the extra ones merged into the most important one.
 * A TV browser may keep the audio suspended until a key is pressed: `blocked()` tells the
 * page to show «premi OK sul telecomando per attivare i suoni», and `unlock()` resumes it.
 */
import type { TvSound, TvSoundEvent, TvSoundKind } from "@/api/tvTypes";

export const MIN_GAP_MS = 4_000;
/** the master gain at volume 100: moderate */
export const MAX_GAIN = 0.5;
const SEEN_MAX = 1_000;

export interface Tone {
  freq: number;
  /** seconds from the sound's start */
  start: number;
  dur: number;
  type: OscillatorType;
}

const note = (freq: number, start: number, dur: number, type: OscillatorType = "sine"): Tone => ({ freq, start, dur, type });

/** buy/sell: one soft chime each (A5 / E5); stop: two notes down; target: two notes up;
 * health red: a low clear tone twice; back to ok: a gentle rising pair */
export const TONES: Record<TvSoundKind, Tone[]> = {
  fill_buy: [note(880, 0, 0.45)],
  fill_sell: [note(659.25, 0, 0.45)],
  stop: [note(659.25, 0, 0.3), note(440, 0.28, 0.5)],
  target: [note(523.25, 0, 0.3), note(783.99, 0.28, 0.5)],
  health_bad: [note(220, 0, 0.5, "triangle"), note(220, 0.6, 0.6, "triangle")],
  health_ok: [note(523.25, 0, 0.25), note(659.25, 0.22, 0.45)],
};

export const PRIORITY: Record<TvSoundKind, number> = {
  health_bad: 5,
  stop: 4,
  target: 3,
  health_ok: 2,
  fill_sell: 1,
  fill_buy: 1,
};

function defaultContext(): AudioContext | null {
  try {
    const w = window as unknown as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext };
    const C = w.AudioContext ?? w.webkitAudioContext;
    return C ? new C() : null;
  } catch {
    return null;
  }
}

export class TvSounds {
  private readonly create: () => AudioContext | null;
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private sound: TvSound | null = null;
  private seen = new Set<string>();
  private lastPlay = -Infinity;
  private pending: TvSoundKind | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private listeners = new Set<() => void>();
  private readonly stateChanged = () => this.listeners.forEach((l) => l());

  constructor(opts: { createContext?: () => AudioContext | null } = {}) {
    this.create = opts.createContext ?? defaultContext;
  }

  /** the user's settings, from every selection poll */
  configure(sound: TvSound): void {
    const wasBlocked = this.blocked();
    this.sound = sound;
    if (!sound.enabled) {
      this.cancelPending();
      this.closeContext();
    } else if (!this.ctx) {
      this.ctx = this.create();
      if (this.ctx) {
        this.master = this.ctx.createGain();
        this.master.connect(this.ctx.destination);
        this.ctx.addEventListener("statechange", this.stateChanged);
      }
    }
    if (this.master) this.master.gain.value = (Math.max(0, Math.min(100, sound.volume)) / 100) * MAX_GAIN;
    if (sound.quiet_now) this.cancelPending();
    if (wasBlocked !== this.blocked()) this.stateChanged();
  }

  /** sounds on, but the browser keeps the audio suspended */
  blocked(): boolean {
    return !!this.sound?.enabled && this.ctx !== null && this.ctx.state !== "running";
  }

  onChange(cb: () => void): () => void {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  }

  /** a key or click: the browser lets the audio start */
  unlock(): void {
    if (this.ctx && this.ctx.state === "suspended") {
      this.ctx.resume().then(this.stateChanged, () => {});
    }
  }

  seenCount(): number {
    return this.seen.size;
  }

  /** the first look: these events are already known, none of them plays */
  baseline(events: TvSoundEvent[]): void {
    events.forEach((e) => this.seen.add(e.id));
  }

  /** the poll's sound events: each id once; dropped when off, quiet or blocked */
  handle(events: TvSoundEvent[]): void {
    let best: TvSoundKind | null = null;
    for (const e of events) {
      if (this.seen.has(e.id)) continue;
      this.seen.add(e.id);
      if (!(e.kind in TONES)) continue;
      if (best === null || PRIORITY[e.kind] > PRIORITY[best]) best = e.kind;
    }
    if (this.seen.size > SEEN_MAX) this.seen = new Set([...this.seen].slice(-SEEN_MAX / 2));
    if (best === null || !this.allowed()) return;
    const now = Date.now();
    if (this.timer === null && now - this.lastPlay >= MIN_GAP_MS) {
      this.play(best);
      return;
    }
    if (this.pending === null || PRIORITY[best] > PRIORITY[this.pending]) this.pending = best;
    if (this.timer === null) {
      this.timer = setTimeout(() => {
        this.timer = null;
        const kind = this.pending;
        this.pending = null;
        if (kind && this.allowed()) this.play(kind);
      }, Math.max(0, this.lastPlay + MIN_GAP_MS - now));
    }
  }

  destroy(): void {
    this.cancelPending();
    this.closeContext();
    this.listeners.clear();
  }

  private allowed(): boolean {
    return !!this.sound?.enabled && !this.sound.quiet_now && this.ctx !== null && this.ctx.state === "running" && this.master !== null;
  }

  private play(kind: TvSoundKind): void {
    const ctx = this.ctx!;
    this.lastPlay = Date.now();
    const t0 = ctx.currentTime + 0.02;
    for (const tone of TONES[kind]) {
      const osc = ctx.createOscillator();
      const env = ctx.createGain();
      osc.type = tone.type;
      osc.frequency.setValueAtTime(tone.freq, t0);
      const start = t0 + tone.start;
      env.gain.setValueAtTime(0.0001, start);
      env.gain.linearRampToValueAtTime(1, start + 0.02);
      env.gain.exponentialRampToValueAtTime(0.0001, start + tone.dur);
      osc.connect(env);
      env.connect(this.master!);
      osc.onended = () => {
        osc.disconnect();
        env.disconnect();
      };
      osc.start(start);
      osc.stop(start + tone.dur + 0.05);
    }
  }

  private cancelPending(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.pending = null;
  }

  private closeContext(): void {
    if (!this.ctx) return;
    this.ctx.removeEventListener("statechange", this.stateChanged);
    this.ctx.close().catch(() => {});
    this.ctx = null;
    this.master = null;
  }
}
