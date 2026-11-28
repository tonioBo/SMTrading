import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { TvSelection } from "@/api/tvTypes";
import {
  CODE_LIMITED_MS,
  CODE_TEXT,
  codePollMs,
  codeState,
  DAILY_RELOAD_MS,
  isRevoked,
  pollMs,
  requestCode,
  retryMs,
  SCREEN_LABELS,
  spacedCode,
  TvFetchError,
  tvEnv,
  tvGet,
  tvTransport,
} from "@/tv/display";
import { FlowScreen } from "@/tv/FlowScreen";
import { CampaignScreen, HealthScreen } from "@/tv/screens";
import { TvSounds } from "@/tv/sounds";
import { exitApp, keyAction, KEYS_LEGEND, registerKeys } from "@/tvapp/keys";
import { initialPair, pairReducer } from "@/tvapp/pairing";
import { DEFAULT_BASE, loadStored, normalizeBase, saveStored } from "@/tvapp/storage";
import { fromSelection, selectionKey, viewStep, type ViewState } from "@/tvapp/view";

/**
 * «SMTrading TV»: the Tizen app (tv-app/) showing the TV screens full screen. First start:
 * the server address (the app's http entry, port 8087); then the 6-digit pairing code
 * («Schermo TV → Abbina con codice» in the portal), whose answer brings the display token,
 * kept in localStorage and sent in X-SMT-TV-Token on every call. The portal's selection is
 * read every 3–5 s; the remote changes the view until the portal changes it again.
 */

const STAGE_BG = "radial-gradient(1200px 700px at 20% 30%, rgba(90,80,255,.14), transparent 60%), linear-gradient(180deg,#070b19,#03050c)";
const CLOCK = new Intl.DateTimeFormat("it-IT", { timeZone: "Europe/Rome", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const NOTE_MS = 4_000;
const LEGEND_MS = 12_000;
export const HEALTH_PATH = "/tv-app/health";
export const WRONG_ENTRY_TEXT =
  "Il server non ha dato il collegamento all'app: l'indirizzo deve essere l'ingresso dell'app TV (porta 8087). Premi il tasto blu per cambiarlo.";

function useClock(): string {
  const [now, setNow] = useState(() => CLOCK.format(new Date()));
  useEffect(() => {
    const id = setInterval(() => setNow(CLOCK.format(new Date())), 15_000);
    return () => clearInterval(id);
  }, []);
  return now;
}

function Setup({ initial, onDone, canCancel, onCancel }: { initial: string; onDone: (base: string) => void; canCancel: boolean; onCancel: () => void }) {
  const [text, setText] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => input.current?.focus(), []);

  const submit = async () => {
    const base = normalizeBase(text);
    if (!base) {
      setError("Indirizzo non valido: scrivi per esempio http://192.168.30.7:8087");
      return;
    }
    setChecking(true);
    setError(null);
    try {
      const res = await fetch(base + HEALTH_PATH, { cache: "no-store", credentials: "omit" });
      if (!res.ok) throw new Error(String(res.status));
      onDone(base);
    } catch {
      setError(`Nessuna risposta da ${base}: controlla l'indirizzo e che SMTrading sia acceso`);
    } finally {
      setChecking(false);
    }
  };

  return (
    <form
      className="m-auto w-[70%] text-center"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      onKeyDown={(e) => {
        if (canCancel && keyAction(e.nativeEvent) === "back") {
          e.preventDefault();
          e.stopPropagation();
          onCancel();
        }
      }}
    >
      <h1 className="m-0 text-[2.6em] font-semibold">SMTrading TV</h1>
      <p className="mt-[0.6em] text-[1.6em] text-[#a9b5dc]">Indirizzo di SMTrading per l'app TV</p>
      <input
        ref={input}
        aria-label="Indirizzo del server"
        value={text}
        onChange={(e) => setText(e.target.value)}
        inputMode="url"
        autoComplete="off"
        spellCheck={false}
        className="mt-[0.6em] w-full rounded-[0.4em] border-2 border-white/40 bg-white/10 px-[0.6em] py-[0.3em] font-mono text-[2em] text-white outline-none focus:border-[#3ee6a0]"
      />
      <button type="submit" disabled={checking} className="mt-[0.8em] rounded-[0.4em] bg-[#059669] px-[1.2em] py-[0.35em] text-[1.6em] font-semibold text-white focus:outline focus:outline-4 focus:outline-white">
        {checking ? "Verifica…" : "Usa questo indirizzo"}
      </button>
      {error && (
        <p role="alert" className="mt-[0.8em] text-[1.4em] text-[#ffc24d]">
          {error}
        </p>
      )}
      <p className="mt-[1.2em] text-[1.1em] text-[#a9b5dc]">
        Di solito non serve cambiarlo: premi OK. {canCancel && "Indietro: torna senza cambiare."}
      </p>
    </form>
  );
}

export default function TvApp() {
  const [pair, dispatch] = useReducer(pairReducer, undefined, () => initialPair(loadStored()));
  const [sel, setSel] = useState<TvSelection | null>(null);
  const sounds = useMemo(() => new TvSounds(), []);
  const [blocked, setBlocked] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [legend, setLegend] = useState(true);
  const clock = useClock();

  // every display call: the app's server and its token in a header, never a cookie
  const tokenRef = useRef(pair.token);
  tokenRef.current = pair.token;
  tvTransport.base = pair.base ?? "";
  tvTransport.credentials = "omit";
  tvTransport.headers = () => ({ "X-SMT-TV-App": "1", ...(tokenRef.current ? { "X-SMT-TV-Token": tokenRef.current } : {}) });

  useEffect(() => saveStored({ base: pair.base, token: pair.token }), [pair.base, pair.token]);

  useEffect(() => {
    document.title = "SMTrading TV";
    registerKeys();
    const off = sounds.onChange(() => setBlocked(sounds.blocked()));
    const unlock = () => sounds.unlock();
    window.addEventListener("keydown", unlock);
    return () => {
      off();
      window.removeEventListener("keydown", unlock);
      sounds.destroy();
    };
  }, [sounds]);

  useEffect(() => {
    const id = setTimeout(() => setLegend(false), LEGEND_MS);
    return () => clearTimeout(id);
  }, []);

  useEffect(() => {
    if (!note) return;
    const id = setTimeout(() => setNote(null), NOTE_MS);
    return () => clearTimeout(id);
  }, [note]);

  // the selection, read while the app has a token (connecting → live)
  const reading = pair.phase === "connecting" || pair.phase === "live";
  useEffect(() => {
    if (!reading) return;
    const ctl = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let failures = 0;
    let cursor: string | null = null;
    const startedAt = Date.now();
    const poll = async () => {
      try {
        const qs = cursor ? `?after=${encodeURIComponent(cursor)}` : "";
        const s = await tvGet<TvSelection>(`/api/tv-display/selection${qs}`, ctl.signal);
        if (ctl.signal.aborted) return;
        if (Date.now() - startedAt > DAILY_RELOAD_MS) {
          tvEnv.reload();
          return;
        }
        const first = cursor === null;
        cursor = s.sound_cursor;
        failures = 0;
        sounds.configure(s.sound);
        if (first) sounds.baseline(s.sound_events);
        else sounds.handle(s.sound_events);
        setBlocked(sounds.blocked());
        setSel(s);
        dispatch({ type: "live" });
        timer = setTimeout(poll, pollMs(s));
      } catch (err) {
        if (ctl.signal.aborted) return;
        if (isRevoked(err)) {
          setSel(null);
          dispatch({ type: "unauthorized" });
          return;
        }
        dispatch({ type: "offline" });
        timer = setTimeout(poll, retryMs(++failures));
      }
    };
    void poll();
    return () => {
      ctl.abort();
      if (timer) clearTimeout(timer);
    };
  }, [reading, pair.base, pair.token, sounds]);

  // no token: a code to show, then its state until a user writes it in the portal
  const pollId = pair.code?.poll_id ?? null;
  useEffect(() => {
    if (pair.phase !== "code" || pair.wrongEntry) return;
    const ctl = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let failures = 0;
    const ask = async () => {
      try {
        const c = await requestCode(ctl.signal);
        if (ctl.signal.aborted) return;
        dispatch({ type: "code", code: c });
      } catch (err) {
        if (ctl.signal.aborted) return;
        dispatch({ type: "offline" });
        timer = setTimeout(ask, err instanceof TvFetchError && err.status === 429 ? CODE_LIMITED_MS : retryMs(++failures));
      }
    };
    const follow = async () => {
      try {
        const r = await codeState(pollId!, ctl.signal);
        if (ctl.signal.aborted) return;
        failures = 0;
        dispatch({ type: "online" });
        if (r.state === "claimed") dispatch({ type: "claimed", token: r.token });
        else if (r.state === "expired") dispatch({ type: "expired" });
        else timer = setTimeout(follow, codePollMs(pair.code));
      } catch {
        if (ctl.signal.aborted) return;
        dispatch({ type: "offline" });
        timer = setTimeout(follow, retryMs(++failures));
      }
    };
    if (pollId === null) void ask();
    else timer = setTimeout(follow, codePollMs(pair.code));
    return () => {
      ctl.abort();
      if (timer) clearTimeout(timer);
    };
    // the code object changes only with its poll id
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pair.phase, pair.base, pollId, pair.wrongEntry]);

  // the view: the portal's choice, changed by the remote until the portal changes it
  const selKey = sel ? selectionKey(sel) : "";
  const [view, setView] = useState<ViewState | null>(null);
  useEffect(() => {
    setView(sel ? fromSelection(sel) : null);
    // a new portal choice only (not every poll)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selKey]);
  const screens = sel?.screens ?? [];
  const interval = Math.max(5, sel?.interval_s ?? 30) * 1000;
  useEffect(() => {
    if (!view || !view.rotating || view.paused) return;
    const id = setTimeout(() => setView((v) => (v ? viewStep(v, "tick", screens).state : v)), interval);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, interval, selKey]);

  // the remote
  const keyState = useRef({ pair, view, screens });
  keyState.current = { pair, view, screens };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const action = keyAction(e);
      if (!action) return;
      const { pair: p, view: v, screens: list } = keyState.current;
      if (p.phase === "setup") {
        // the form handles its keys (typing, OK; Back returns when an address is kept)
        if (action === "back" && p.base === null) exitApp();
        return;
      }
      e.preventDefault();
      if (action === "back") {
        exitApp();
        return;
      }
      if (p.phase !== "live") {
        if (action === "rotation") dispatch({ type: "setup" }); // blue: change the address
        return;
      }
      if (!v) return;
      const out = viewStep(v, action, list);
      setView(out.state);
      setNote(out.note);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const shown = view?.screen ?? null;
  const pos = shown ? screens.indexOf(shown) : -1;

  return (
    <div
      className="fixed inset-0 flex cursor-none select-none flex-col overflow-hidden p-[1.2em] text-white"
      style={{ background: STAGE_BG, fontSize: "max(16px, 1.25vw)", lineHeight: 1.3 }}
    >
      {pair.phase === "setup" ? (
        <Setup
          initial={pair.base ?? DEFAULT_BASE}
          onDone={(base) => dispatch({ type: "configured", base })}
          canCancel={pair.base !== null}
          onCancel={() => dispatch({ type: "configured", base: pair.base! })}
        />
      ) : pair.phase === "code" ? (
        <div className="m-auto max-w-[90%] text-center">
          {pair.revoked && (
            <div role="alert" className="mb-[0.8em] text-[1.8em] font-semibold text-[#ffc24d]">
              Collegamento revocato dal portale: abbina di nuovo la TV
            </div>
          )}
          {pair.wrongEntry ? (
            <div role="alert" className="text-[2em] font-semibold text-[#ffc24d]">
              {WRONG_ENTRY_TEXT}
            </div>
          ) : pair.code ? (
            <>
              <p className="m-0 text-[2em] text-[#a9b5dc]">Abbina questa TV a SMTrading</p>
              <div role="status" aria-label="Codice di abbinamento" className="num my-[0.15em] font-mono text-[11em] font-bold leading-none tracking-[0.12em]">
                {spacedCode(pair.code.code)}
              </div>
              <p className="m-0 text-[2.2em] font-semibold">{CODE_TEXT}</p>
              <p className="mt-[0.8em] text-[1.2em] text-[#a9b5dc]">
                Il codice vale 10 minuti, poi ne compare uno nuovo da solo.
                {pair.offline && " Server non raggiungibile, nuovo tentativo…"}
              </p>
            </>
          ) : (
            <div role="status" className="text-[2.4em] text-[#a9b5dc]">
              {pair.offline ? "Server non raggiungibile, nuovo tentativo…" : "Richiesta del codice di abbinamento…"}
            </div>
          )}
          <p className="mt-[1.6em] text-[1.1em] text-[#a9b5dc]">
            Server: {pair.base} · tasto blu: cambia indirizzo · Indietro: esci
          </p>
        </div>
      ) : !sel || !shown ? (
        <div className="m-auto text-center">
          <div role="status" className="text-[2.4em] text-[#a9b5dc]">
            {pair.offline ? "Server non raggiungibile, nuovo tentativo…" : "Collegamento in corso…"}
          </div>
          <p className="mt-[1.2em] text-[1.1em] text-[#a9b5dc]">
            Server: {pair.base} · tasto blu: cambia indirizzo · Indietro: esci
          </p>
        </div>
      ) : (
        <>
          <header className="mb-[0.8em] flex items-baseline gap-[1em]">
            <h1 className="m-0 text-[2.2em] font-semibold leading-none">{SCREEN_LABELS[shown]}</h1>
            {view?.rotating && screens.length > 1 && (
              <span className="text-[1.1em] text-[#a9b5dc]">
                {view.paused ? "rotazione in pausa" : "rotazione"} {pos + 1} di {screens.length}
              </span>
            )}
            <span className="flex-1" />
            <span className="text-[1.1em] font-semibold tracking-wide text-[#a9b5dc]">SMTrading</span>
            <span className="num text-[2.2em] font-semibold leading-none">{clock}</span>
          </header>
          <main className="flex min-h-0 flex-1 flex-col">
            {shown === "flow" && <FlowScreen key="flow" />}
            {shown === "campaign" && <CampaignScreen key="campaign" />}
            {shown === "health" && <HealthScreen key="health" />}
          </main>
          {(pair.offline || blocked || legend) && (
            <footer className="mt-[0.6em] flex gap-[2em] text-[1em]">
              {pair.offline && (
                <span role="status" className="text-[#ffc24d]">
                  collegamento al server perso, nuovo tentativo…
                </span>
              )}
              {blocked && (
                <span role="status" className="text-[#cfd8f5]">
                  premi OK sul telecomando per attivare i suoni
                </span>
              )}
              {legend && <span className="text-[#a9b5dc]">{KEYS_LEGEND}</span>}
            </footer>
          )}
          {note && (
            <div role="status" className="fixed bottom-[2em] left-1/2 -translate-x-1/2 rounded-[0.6em] bg-black/80 px-[1.2em] py-[0.5em] text-[1.6em] font-semibold">
              {note}
            </div>
          )}
        </>
      )}
    </div>
  );
}
