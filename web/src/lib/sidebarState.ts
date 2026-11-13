/** Desktop sidebar collapsed (icon rail) or expanded, remembered per browser. */
import { useCallback, useState } from "react";

export const SIDEBAR_KEY = "smt-sidebar-collapsed";

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === "1";
  } catch {
    return false; // storage blocked: expanded
  }
}

export function useSidebarCollapsed(): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(SIDEBAR_KEY, next ? "1" : "0");
      } catch {
        // not persisted: the state still applies to this page
      }
      return next;
    });
  }, []);
  return [collapsed, toggle];
}
