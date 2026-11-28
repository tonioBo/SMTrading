import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import type { TvSelection } from "@/api/tvTypes";
import {
  CODE_LIMITED_MS,
  CODE_TEXT,
  codePollMs,
  codeState,
  DAILY_RELOAD_MS,
  isRevoked,
  onNewBuild,
  openSession,
  pollMs,
  requestCode,
  retryMs,
  REVOKED_TEXT,
  spacedCode,
  SCREEN_LABELS,
  screenOrder,
  TvFetchError,
  tvEnv,
  tvGet,
  tokenFromHash,
} from "@/tv/display";
import { FlowScreen } from "@/tv/FlowScreen";
import { CampaignScreen, HealthScreen } from "@/tv/screens";
import { TvSounds } from "@/tv/sounds";

/**
 * The TV pages (`/tv#token=<link>`): no portal chrome, no login. The link is traded once for
 * the display cookie, the fragment is cleared, then the selection made in the portal is read
 * every 3–5 s (screen, rotation, sounds). Failures (an api restart) keep the screen and
 * retry. Without a valid link (a 401: none yet, or revoked) the page shows a 6-digit pairing
 * code, written by the user in the portal («Abbina con codice»), and follows it until it is
 * used: then the display cookie is set and the screens start. The page reloads itself on a new build and once a
 * day (right after a good read), so a browser left on for days starts fresh.
 */

const STAGE_BG = "radial-gradient(1200px 700px at 20% 30%, rgba(90,80,255,.14), transparent 60%), linear-gradient(180deg,#070b19,#03050c)";
const CLOCK = new Intl.DateTimeFormat("it-IT", { timeZone: "Europe/Rome", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

type Phase = "session" | "live" | "code";

function useClock(): string {
  const [now, setNow] = useState(() => CLOCK.format(new Date()));
  useEffect(() => {
    const id = setInterval(() => setNow(CLOCK.format(new Date())), 15_000);
    return () => clearInterval(id);
  }, []);
  return now;
}

export default function TvDisplay() {
  const location = useLocation();
  const navigate = useNavigate();
  const token = useRef(tokenFromHash(location.hash));
  const [phase, setPhase] = useState<Phase>(token.current ? "session" : "live");
  const [sel, setSel] = useState<TvSelection | null>(null);
  const [offline, setOffline] = useState(false);
  /** the code shown while no link opens the screens */
  const [code, setCode] = useState<string | null>(null);
  /** a link refused or revoked (not a TV never paired): said above the code */
  const [revokedNote, setRevokedNote] = useState(false);
  const sounds = useMemo(() => new TvSounds(), []);
  const [blocked, setBlocked] = useState(false);
  const clock = useClock();
  /** bumped when a new link arrives in the fragment: the read loop starts again with it */
  const [run, setRun] = useState(0);

  // a new `#token=` while the page is open (a rotated link): open the session again, no
  // reload needed; router and plain `hashchange` both covered
  const takeToken = useRef<(hash: string) => void>(() => {});
  takeToken.current = (hash: string) => {
    const t = tokenFromHash(hash);
    if (!t || t === token.current) return;
    token.current = t;
    setPhase("session");
    setRun((r) => r + 1);
  };
  useEffect(() => {
    takeToken.current(location.hash);
  }, [location.hash]);
  useEffect(() => {
    const onHash = () => takeToken.current(window.location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    document.title = "SMTrading · Schermo TV";
    const off = sounds.onChange(() => setBlocked(sounds.blocked()));
    const unlock = () => sounds.unlock();
    window.addEventListener("keydown", unlock);
    window.addEventListener("pointerdown", unlock);
    return () => {
      off();
      window.removeEventListener("keydown", unlock);
      window.removeEventListener("pointerdown", unlock);
      sounds.destroy();
    };
  }, [sounds]);

  useEffect(() => {
    const ctl = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let failures = 0;
    let cursor: string | null = null;
    let newBuild = false;
    let wasLive = false;
    const startedAt = Date.now();
    const later = (ms: number, f: () => void) => {
      if (!ctl.signal.aborted) timer = setTimeout(f, ms);
    };
    onNewBuild(() => {
      newBuild = true;
    });

    const poll = async () => {
      try {
        const qs = cursor ? `?after=${encodeURIComponent(cursor)}` : "";
        const s = await tvGet<TvSelection>(`/api/tv-display/selection${qs}`, ctl.signal);
        if (ctl.signal.aborted) return;
        if (newBuild || Date.now() - startedAt > DAILY_RELOAD_MS) {
          tvEnv.reload(); // the server answers right now: the reload will find it
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
        setOffline(false);
        setPhase("live");
        wasLive = true;
        later(pollMs(s), poll);
      } catch (err) {
        if (ctl.signal.aborted) return;
        if (isRevoked(err)) {
          if (wasLive) setRevokedNote(true);
          setSel(null);
          void pairing();
          return;
        }
        setOffline(true);
        later(retryMs(++failures), poll);
      }
    };

    // no link: a code to show, then its state until a user writes it in the portal
    const pairing = async () => {
      setPhase("code");
      try {
        const c = await requestCode(ctl.signal);
        if (ctl.signal.aborted) return;
        failures = 0;
        setCode(c.code);
        setOffline(false);
        const follow = async () => {
          try {
            const r = await codeState(c.poll_id, ctl.signal);
            if (ctl.signal.aborted) return;
            failures = 0;
            setOffline(false);
            if (r.state === "claimed") {
              setCode(null);
              setRevokedNote(false);
              cursor = null; // a fresh start: the first read is the sounds' baseline
              void poll();
            } else if (r.state === "expired") {
              void pairing();
            } else {
              later(codePollMs(c), follow);
            }
          } catch {
            if (ctl.signal.aborted) return;
            setOffline(true);
            later(retryMs(++failures), follow);
          }
        };
        later(codePollMs(c), follow);
      } catch (err) {
        if (ctl.signal.aborted) return;
        setCode(null);
        setOffline(true);
        later(err instanceof TvFetchError && err.status === 429 ? CODE_LIMITED_MS : retryMs(++failures), pairing);
      }
    };

    // the link stays out of the address bar and the history
    const clearHash = () => navigate({ pathname: location.pathname, search: "", hash: "" }, { replace: true });
    const session = async () => {
      const mine = token.current!;
      try {
        await openSession(mine, ctl.signal);
        if (ctl.signal.aborted) return;
        failures = 0;
        clearHash();
        void poll();
      } catch (err) {
        if (ctl.signal.aborted) return;
        if (err instanceof TvFetchError && err.status >= 400 && err.status < 500) {
          clearHash();
          setRevokedNote(true);
          void pairing();
          return;
        }
        setOffline(true);
        later(retryMs(++failures), session);
      }
    };

    // a token stays in `token` (not cleared on success): the same link seen again in the
    // fragment does not restart the loop
    if (token.current) void session();
    else void poll();
    return () => {
      ctl.abort();
      if (timer) clearTimeout(timer);
      onNewBuild(null);
    };
    // once per link: the poll loop owns its state
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run]);

  const order = sel ? screenOrder(sel) : [];
  const rotKey = sel ? `${sel.version}|${order.join(",")}|${sel.interval_s}` : "";
  const [idx, setIdx] = useState(0);
  useEffect(() => {
    setIdx(0);
    if (!sel || order.length < 2) return;
    const id = setInterval(() => setIdx((i) => (i + 1) % order.length), Math.max(5, sel.interval_s) * 1000);
    return () => clearInterval(id);
    // the rotation restarts when the selection changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rotKey]);
  const shown = order.length ? order[idx % order.length] : null;

  return (
    <div
      className="fixed inset-0 flex cursor-none select-none flex-col overflow-hidden p-[1.2em] text-white"
      style={{ background: STAGE_BG, fontSize: "max(16px, 1.2vw)", lineHeight: 1.3 }}
    >
      {phase === "code" ? (
        <div className="m-auto max-w-[90%] text-center">
          {revokedNote && (
            <div role="alert" className="mb-[0.8em] text-[1.8em] font-semibold text-[#ffc24d]">
              {REVOKED_TEXT[0].toUpperCase() + REVOKED_TEXT.slice(1)}
            </div>
          )}
          {code ? (
            <>
              <p className="m-0 text-[2em] text-[#a9b5dc]">Abbina questa TV a SMTrading</p>
              <div
                role="status"
                aria-label="Codice di abbinamento"
                className="num my-[0.15em] font-mono text-[11em] font-bold leading-none tracking-[0.12em]"
              >
                {spacedCode(code)}
              </div>
              <p className="m-0 text-[2.2em] font-semibold">{CODE_TEXT}</p>
              <p className="mt-[0.8em] text-[1.2em] text-[#a9b5dc]">
                Il codice vale 10 minuti, poi ne compare uno nuovo da solo.
                {offline && " Server non raggiungibile, nuovo tentativo…"}
              </p>
            </>
          ) : (
            <div role="status" className="text-[2.4em] text-[#a9b5dc]">
              {offline ? "Server non raggiungibile, nuovo tentativo…" : "Richiesta del codice di abbinamento…"}
            </div>
          )}
        </div>
      ) : !sel || !shown ? (
        <div role="status" className="m-auto text-[2.4em] text-[#a9b5dc]">
          {offline ? "Server non raggiungibile, nuovo tentativo…" : "Collegamento in corso…"}
        </div>
      ) : (
        <>
          <header className="mb-[0.8em] flex items-baseline gap-[1em]">
            <h1 className="m-0 text-[2.2em] font-semibold leading-none">{SCREEN_LABELS[shown]}</h1>
            {order.length > 1 && (
              <span className="text-[1.1em] text-[#a9b5dc]">
                rotazione {idx + 1} di {order.length}
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
          {(offline || blocked) && (
            <footer className="mt-[0.6em] flex gap-[2em] text-[1em]">
              {offline && (
                <span role="status" className="text-[#ffc24d]">
                  collegamento al server perso, nuovo tentativo…
                </span>
              )}
              {blocked && (
                <span role="status" className="text-[#cfd8f5]">
                  premi OK sul telecomando per attivare i suoni
                </span>
              )}
            </footer>
          )}
        </>
      )}
    </div>
  );
}
