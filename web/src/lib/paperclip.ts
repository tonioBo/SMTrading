import type { PaperclipStatus } from "@/api/types";
import { formatDateTime, formatDecimal, formatMoney } from "@/lib/format";

/** the reader alerts on the 3rd consecutive failed read: the banner follows the same threshold */
export const PAPERCLIP_ERRORS_THRESHOLD = 3;

export function isolationText(changes: PaperclipStatus["changes"]): string {
  return `Le regole di isolamento di Paperclip sono cambiate: ${changes.removed.length} rimosse, ${changes.changed.length} modificate`;
}

export function expiryText(expiresAt: string | null, subject = "La chiave board di Paperclip"): string {
  if (!expiresAt) return `${subject} sta per scadere`;
  return new Date(expiresAt).getTime() <= Date.now()
    ? `${subject} è scaduta il ${formatDateTime(expiresAt)}: creane una nuova`
    : `${subject} scade il ${formatDateTime(expiresAt)}: creane una nuova`;
}

/**
 * The reader is on but has no expected isolation rules (none found, or not read yet): the
 * alarm cannot fire. Not shown while the rules are re-read after a configuration change.
 */
export function isolationInactive(status: PaperclipStatus): boolean {
  return status.enabled && status.isolation === "unknown" && status.rebaseline_reason !== "config";
}

function origin(url: string): string | null {
  try {
    const u = new URL(url.trim());
    return u.hostname ? `${u.hostname}:${u.port || (u.protocol === "http:" ? "80" : "443")}` : null;
  } catch {
    return null;
  }
}

/**
 * The typed address is on another host (or port) than the saved one: the server keeps the
 * stored board key only for the same host, so a new key is needed. False while the typed
 * text is not a URL yet, or nothing was saved.
 */
export function hostChanged(saved: string | null, typed: string): boolean {
  const next = origin(typed);
  return next !== null && next !== (saved ? origin(saved) : null);
}

/**
 * USD as Paperclip reports it, it-IT format: "1.234,50 $"; below 1 $ three decimals ("0,123 $"),
 * a non-zero amount under half a cent "< 0,01 $".
 */
export function formatUsd(value: string): string {
  const abs = Math.abs(Number(value)); // only to pick the format: the digits come from the string
  if (!Number.isFinite(abs) || abs === 0 || abs >= 1) return formatMoney(value, "USD");
  if (abs < 0.005) return "< 0,01 $";
  return `${formatDecimal(value, 3)} $`;
}
