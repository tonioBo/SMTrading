import type { PaperclipStatus } from "@/api/types";
import { formatDateTime } from "@/lib/format";

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
