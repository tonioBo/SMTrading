import { useId, type ReactElement, type ReactNode } from "react";
import { cloneElement } from "react";
import { Label } from "@/components/ui/label";

/** Label + control + inline error; the control gets the id, aria-invalid and aria-describedby. */
export function FormField({
  label,
  error,
  hint,
  children,
}: {
  label: string;
  error?: string | null;
  hint?: ReactNode;
  children: ReactElement;
}) {
  const id = useId();
  const errorId = `${id}-error`;
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      {cloneElement(children, {
        id,
        "aria-invalid": error ? true : undefined,
        "aria-describedby": error ? errorId : undefined,
      })}
      {hint && !error && <p className="text-xs text-muted">{hint}</p>}
      {error && (
        <p id={errorId} className="text-xs text-loss">
          {error}
        </p>
      )}
    </div>
  );
}

export const selectClass =
  "flex h-9 w-full rounded-md border border-border bg-bg px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent disabled:opacity-50";
