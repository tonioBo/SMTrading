import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import type { TvSelection } from "@/api/tvTypes";
import {
  DAILY_RELOAD_MS,
  isRevoked,
  onNewBuild,
  openSession,
  pollMs,
  retryMs,
  REVOKED_RETRY_MS,
  REVOKED_TEXT,
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
 * retry; a 401 means the link was revoked. The page reloads itself on a new build and once a
 * day (right after a good read), so a browser left on for days starts fresh.
 */

const STAGE_BG = "radial-gradient(1200px 700px at 20% 30%, rgba(90,80,255,.14), transparent 60%), linear-gradient(180deg,#070b19,#03050c)";
const CLOCK = new Intl.DateTimeFormat("it-IT", { timeZone: "Europe/Rome", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

type Phase = "session" | "live" | "revoked";

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
        later(pollMs(s), poll);
      } catch (err) {
        if (ctl.signal.aborted) return;
        if (isRevoked(err)) {
          setPhase("revoked");
          later(REVOKED_RETRY_MS, poll);
          return;
        }
        setOffline(true);
        later(retryMs(++failures), poll);
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
          setPhase("revoked");
          later(REVOKED_RETRY_MS, poll);
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
      {phase === "revoked" ? (
        <div role="alert" className="m-auto max-w-[80%] text-center text-[2.6em] font-semibold">
          {REVOKED_TEXT[0].toUpperCase() + REVOKED_TEXT.slice(1)}
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
