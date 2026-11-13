import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { throttle } from "@/lib/throttle";

describe("throttle", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("runs at once, then at most once per window with a trailing run", () => {
    const fn = vi.fn();
    const t = throttle(fn, 3000);
    t.call();
    expect(fn).toHaveBeenCalledTimes(1); // leading
    t.call();
    t.call();
    t.call();
    expect(fn).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(2999);
    expect(fn).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1);
    expect(fn).toHaveBeenCalledTimes(2); // one trailing run for the burst
    vi.advanceTimersByTime(10_000);
    expect(fn).toHaveBeenCalledTimes(2);
    t.call();
    expect(fn).toHaveBeenCalledTimes(3); // window passed: leading again
  });

  it("cancel drops the pending trailing run", () => {
    const fn = vi.fn();
    const t = throttle(fn, 1000);
    t.call();
    t.call();
    t.cancel();
    vi.advanceTimersByTime(5000);
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
