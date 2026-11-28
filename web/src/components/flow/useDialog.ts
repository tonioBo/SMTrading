import { useEffect, useRef, type RefObject } from "react";

/** the panels open over the river, newest last: Escape closes only the top one */
const stack: symbol[] = [];

/** A panel over the river (story, lens): focus moves into it when it opens and back to
 * where it was when it closes; Escape closes it only when it is the top panel. */
export function useDialog(ref: RefObject<HTMLElement>, onClose: () => void): void {
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const me = Symbol("dialog");
    stack.push(me);
    const before = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || stack[stack.length - 1] !== me) return;
      e.stopImmediatePropagation();
      close.current();
    };
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      stack.splice(stack.indexOf(me), 1);
      if (before && before.isConnected && typeof before.focus === "function") before.focus();
    };
  }, [ref]);
}
