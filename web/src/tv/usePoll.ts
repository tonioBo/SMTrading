import { useEffect, useRef, useState } from "react";
import { retryMs, TvFetchError, tvGet } from "@/tv/display";

export interface PollState<T> {
  data: T | null;
  /** the last read failed: the screen keeps what it has and tries again */
  failed: boolean;
  /** HTTP status of the last failure (0 = network) */
  status: number | null;
}

/**
 * Reads `path(previous)` now and then every `everyMs` (a setTimeout chain: one request at a
 * time, aborted on unmount). Failures back off (5 → 30 s) and keep the last data, so a
 * restarting api shows the old figures until it answers again. Only the latest answer is
 * kept: nothing grows over days.
 */
export function usePoll<T>(path: (prev: T | null) => string, everyMs: number, onData?: (d: T) => void): PollState<T> {
  const [state, setState] = useState<PollState<T>>({ data: null, failed: false, status: null });
  const pathRef = useRef(path);
  pathRef.current = path;
  const onDataRef = useRef(onData);
  onDataRef.current = onData;

  useEffect(() => {
    const ctl = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let prev: T | null = null;
    let failures = 0;
    const tick = async () => {
      try {
        const d = await tvGet<T>(pathRef.current(prev), ctl.signal);
        if (ctl.signal.aborted) return;
        prev = d;
        failures = 0;
        onDataRef.current?.(d);
        setState({ data: d, failed: false, status: null });
        timer = setTimeout(tick, everyMs);
      } catch (err) {
        if (ctl.signal.aborted) return;
        failures++;
        const status = err instanceof TvFetchError ? err.status : 0;
        setState((s) => ({ data: s.data, failed: true, status }));
        timer = setTimeout(tick, Math.max(everyMs, retryMs(failures)));
      }
    };
    void tick();
    return () => {
      ctl.abort();
      if (timer) clearTimeout(timer);
    };
  }, [everyMs]);

  return state;
}
