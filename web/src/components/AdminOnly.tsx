import type { ReactNode } from "react";
import { useIsAdmin } from "@/api/hooks/auth";

/** Renders its children for admins only: a «Sola lettura» user sees the same pages without
 * the edit, create and delete controls. */
export function AdminOnly({ children }: { children: ReactNode }) {
  return useIsAdmin() ? <>{children}</> : null;
}

/** Its form controls are disabled for a «Sola lettura» user (an editor stays readable). */
export function AdminFieldset({ children, label }: { children: ReactNode; label?: string }) {
  const admin = useIsAdmin();
  return (
    <fieldset disabled={!admin} aria-label={label} className="m-0 min-w-0 border-0 p-0">
      {children}
    </fieldset>
  );
}
