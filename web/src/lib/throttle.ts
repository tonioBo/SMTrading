/**
 * Leading + trailing throttle: the first call runs at once, calls during the next `ms`
 * collapse into one run at the end of the window. Never more than one run per `ms`.
 */
export interface Throttled {
  call: () => void;
  cancel: () => void;
}

export function throttle(fn: () => void, ms: number): Throttled {
  let last = -Infinity;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const run = () => {
    last = Date.now();
    fn();
  };
  return {
    call() {
      const wait = last + ms - Date.now();
      if (wait <= 0 && timer === null) {
        run();
      } else if (timer === null) {
        timer = setTimeout(() => {
          timer = null;
          run();
        }, Math.max(wait, 0));
      }
    },
    cancel() {
      if (timer !== null) clearTimeout(timer);
      timer = null;
    },
  };
}
