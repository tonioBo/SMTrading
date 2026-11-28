import { useState } from "react";
import { cn } from "@/lib/utils";
import { fromRomeInput, PRESETS, presetRange, REPLAY_MAX_MS, REPLAY_SPEEDS, toRomeInput, type Range, type ReplaySpeed } from "./replay";
import { BTN, BTN_ON, clock, dayClock, GLASS, num } from "./styles";

/** «Moviola»: the bar under the header — a past window (presets or from–to, Rome time,
 * at most 24 h), play/pause, 1×–300×, a scrub over the window and the time shown. */

export interface ReplayBarProps {
  /** the window being replayed, null before one is chosen */
  range: Range | null;
  /** the time shown (server ms) */
  shown: number | null;
  playing: boolean;
  speed: ReplaySpeed;
  loading: { pages: number; events: number; done: boolean; capped: boolean; error: string | null };
  now: () => number;
  /** the last US session from SMTrading's calendar (GET /api/flow/map), if known */
  lastSession?: Range | null;
  onRange: (r: Range) => void;
  onPlay: (b: boolean) => void;
  onSpeed: (s: ReplaySpeed) => void;
  onSeek: (t: number) => void;
  onClose: () => void;
}

const INPUT =
  "h-[30px] rounded-[9px] border border-white/15 bg-white/[.06] px-2 text-xs text-[#e8eeff] [color-scheme:dark] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#34e0c4]";

export function ReplayBar({ range, shown, playing, speed, loading, now, lastSession, onRange, onPlay, onSpeed, onSeek, onClose }: ReplayBarProps) {
  const [from, setFrom] = useState(() => toRomeInput(range?.from ?? now() - 3_600_000));
  const [to, setTo] = useState(() => toRomeInput(range?.to ?? now()));
  const [invalid, setInvalid] = useState<string | null>(null);

  const apply = () => {
    const f = fromRomeInput(from);
    const t = fromRomeInput(to);
    if (f === null || t === null) return setInvalid("date non valide");
    if (f > now()) return setInvalid("«da» non può essere nel futuro");
    if (f >= t) return setInvalid("«da» deve venire prima di «a»");
    if (t - f > REPLAY_MAX_MS) return setInvalid("al massimo 24 ore per volta");
    setInvalid(null);
    onRange({ from: f, to: Math.min(t, now()) });
  };
  const preset = (key: (typeof PRESETS)[number]["key"]) => {
    const r = presetRange(key, now(), lastSession);
    setFrom(toRomeInput(r.from));
    setTo(toRomeInput(r.to));
    setInvalid(null);
    onRange(r);
  };

  let status: string | null = null;
  if (range && !loading.error) {
    if (!loading.done) status = `caricamento… ${num(loading.events, 0)} passaggi`;
    else if (loading.capped) status = `intervallo molto fitto: mostrati i primi ${num(loading.events, 0)} passaggi`;
    else status = `${num(loading.events, 0)} passaggi nell'intervallo`;
  }

  return (
    <div role="region" aria-label="Moviola" className={cn(GLASS, "flex flex-wrap items-center gap-x-4 gap-y-2 border-[rgba(215,139,255,.3)] px-4 py-2.5 text-[11.5px] text-[#8b99c3]")}>
      <div className="whitespace-nowrap text-[13px] font-semibold text-[#e3c4ff]">
        {range && shown !== null ? `moviola · ora mostrata ${dayClock(shown)}` : "moviola · scegli un intervallo"}
      </div>
      <div role="group" aria-label="Intervalli pronti" className="flex flex-wrap gap-1">
        {PRESETS.map((p) => (
          <button key={p.key} type="button" className={BTN} onClick={() => preset(p.key)}>
            {p.label}
          </button>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <label className="flex items-center gap-1">
          da
          <input type="datetime-local" className={INPUT} value={from} onChange={(e) => setFrom(e.target.value)} aria-label="da" />
        </label>
        <label className="flex items-center gap-1">
          a
          <input type="datetime-local" className={INPUT} value={to} onChange={(e) => setTo(e.target.value)} aria-label="a" />
        </label>
        <button type="button" className={BTN} onClick={apply}>
          Mostra
        </button>
      </div>
      {range && (
        <>
          <button type="button" className={cn(BTN, playing && BTN_ON)} aria-pressed={playing} onClick={() => onPlay(!playing)}>
            {playing ? "⏸ Pausa" : "▶ Riproduci"}
          </button>
          <div role="group" aria-label="Velocità della moviola" className="flex items-center gap-1">
            {REPLAY_SPEEDS.map((s) => (
              <button key={s} type="button" className={cn(BTN, speed === s && BTN_ON)} aria-pressed={speed === s} aria-label={`velocità ${s}×`} onClick={() => onSpeed(s)}>
                {s}×
              </button>
            ))}
          </div>
          <input
            type="range"
            aria-label="ora mostrata"
            className="min-w-[160px] flex-1 accent-[#d78bff]"
            min={range.from}
            max={range.to}
            step={1000}
            value={shown ?? range.from}
            aria-valuetext={clock(shown ?? range.from)}
            onChange={(e) => onSeek(Number(e.target.value))}
          />
        </>
      )}
      {status && (
        <span role="status" className="whitespace-nowrap">
          {status}
        </span>
      )}
      {(invalid || (range && loading.error)) && (
        <span role="alert" className="whitespace-nowrap text-[#ffb3c1]">
          {invalid ?? loading.error}
        </span>
      )}
      <div className="flex-1" />
      <button type="button" className={BTN} onClick={onClose}>
        Torna al vivo
      </button>
    </div>
  );
}
