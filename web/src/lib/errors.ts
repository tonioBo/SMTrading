import { ApiError } from "@/api/client";

export function errorMessage(err: unknown, fallback = "Operazione non riuscita"): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

/** The form field an API error refers to (`details.field`), if any. */
export function errorField(err: unknown): string | null {
  if (!(err instanceof ApiError) || typeof err.details !== "object" || err.details === null) return null;
  const field = (err.details as { field?: unknown }).field;
  return typeof field === "string" ? field : null;
}
