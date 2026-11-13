/** "+01:00" style UTC offset of `timeZone` at the instant `utcMs`. */
function offsetAt(utcMs: number, timeZone: string): string {
  const name = new Intl.DateTimeFormat("en-US", { timeZone, timeZoneName: "longOffset" })
    .formatToParts(new Date(utcMs))
    .find((p) => p.type === "timeZoneName")?.value ?? "GMT";
  const m = /GMT([+-]\d{2}):?(\d{2})?/.exec(name);
  return m ? `${m[1]}:${m[2] ?? "00"}` : "+00:00";
}

function offsetMinutes(offset: string): number {
  const sign = offset.startsWith("-") ? -1 : 1;
  const [h, m] = offset.slice(1).split(":").map(Number);
  return sign * (h * 60 + m);
}

/**
 * ISO 8601 with the offset `timeZone` has at that wall-clock time, e.g.
 * zonedIso("2026-11-12", "00:00:00", "Europe/Rome") → "2026-11-12T00:00:00+01:00".
 */
export function zonedIso(date: string, time: string, timeZone: string): string {
  const wallUtc = Date.parse(`${date}T${time}Z`);
  // the offset at the wall time read as UTC, then re-checked at the instant it gives (DST)
  let offset = offsetAt(wallUtc, timeZone);
  offset = offsetAt(wallUtc - offsetMinutes(offset) * 60_000, timeZone);
  return `${date}T${time}${offset}`;
}

/** First and last instant of a calendar day (yyyy-mm-dd) in `timeZone`. */
export const zonedDayStart = (date: string, timeZone: string) => zonedIso(date, "00:00:00", timeZone);
export const zonedDayEnd = (date: string, timeZone: string) => zonedIso(date, "23:59:59.999", timeZone);
