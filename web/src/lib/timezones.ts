const FALLBACK_TIMEZONES = ["Europe/Rome", "Europe/London", "Europe/Paris", "Europe/Berlin", "America/New_York", "America/Chicago", "America/Los_Angeles", "Asia/Tokyo", "UTC"];

/** The IANA timezones of the browser (a short fallback list), always including `current`. */
export function timezones(current: string): string[] {
  let list = FALLBACK_TIMEZONES;
  try {
    const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf?.("timeZone");
    if (supported?.length) list = supported;
  } catch {
    // keep the fallback list
  }
  return list.includes(current) ? list : [current, ...list];
}
