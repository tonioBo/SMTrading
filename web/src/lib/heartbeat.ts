/** Worker heartbeat age → status: green < 30 s, amber < 120 s, red otherwise or unknown. */
export const HEARTBEAT_OK_S = 30;
export const HEARTBEAT_WARN_S = 120;

export function heartbeatState(age: number | null): { tone: "ok" | "warn" | "down"; label: string } {
  if (age === null) return { tone: "down", label: "Worker non raggiungibile" };
  const secs = `${Math.round(age)} s fa`;
  if (age < HEARTBEAT_OK_S) return { tone: "ok", label: `Worker attivo (${secs})` };
  if (age < HEARTBEAT_WARN_S) return { tone: "warn", label: `Worker in ritardo (${secs})` };
  return { tone: "down", label: `Worker fermo (${secs})` };
}
