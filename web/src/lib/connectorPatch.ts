import type { Connector, ConnectorPatch } from "@/api/types";

/** Whether a top-level config key holds a secret: the server says so (`secret_keys`). */
export const isSecret = (connector: Connector, key: string) => connector.secret_keys.includes(key);
export const isScalar = (v: unknown): v is string | number | null => v === null || typeof v === "string" || typeof v === "number";

/** The fields the card shows although the stored config lacks them: `config_fields`, or `api_key` when the connector requires a key and declares none. */
export function emptyFields(connector: Connector): string[] {
  const declared = connector.config_fields ?? [];
  const wanted = declared.length ? declared : connector.requires_key ? ["api_key"] : [];
  return [...new Set(wanted)].filter((k) => !(k in connector.config_masked));
}

/** The text a quota field starts from: the admin's value ("0" = none), "" for the default. */
export function budgetText(connector: Connector, key: "per_hour" | "per_day"): string {
  const q = connector.quota;
  if (!q || !q.custom.includes(key)) return "";
  return String(q[key] ?? 0);
}

/** The PATCH body for what the user changed: untouched masked secrets are left out (the server keeps them). */
export function buildConnectorPatch(
  connector: Connector,
  edits: { rate: string; fields: Record<string, string>; removed: Set<string>; hourly?: string; daily?: string },
): { patch: ConnectorPatch; error: string | null } {
  const patch: ConnectorPatch = {};
  const rate = edits.rate.trim();
  if (rate !== String(connector.rate_limit_per_min ?? "")) {
    const n = Number(rate);
    if (!/^\d+$/.test(rate) || n < 1) return { patch, error: "Limite di richieste non valido" };
    patch.rate_limit_per_min = n;
  }
  for (const [text, key, field] of [
    [edits.hourly, "per_hour", "hourly_budget"],
    [edits.daily, "per_day", "daily_budget"],
  ] as const) {
    if (text === undefined) continue;
    const value = text.trim();
    if (value === budgetText(connector, key)) continue;
    if (value !== "" && !/^\d+$/.test(value)) return { patch, error: "Quota non valida" };
    patch[field] = value === "" ? null : Number(value);
  }
  const config: Record<string, unknown> = {};
  let dirty = false;
  for (const [key, stored] of Object.entries(connector.config_masked)) {
    if (isSecret(connector, key) && isScalar(stored)) {
      if (edits.removed.has(key)) {
        config[key] = null;
        dirty = true;
      } else if (edits.fields[key]) {
        config[key] = edits.fields[key];
        dirty = true;
      }
    } else if (isScalar(stored) && key in edits.fields) {
      const text = edits.fields[key];
      if (typeof stored === "number") {
        if (text.trim() === "" || Number.isNaN(Number(text))) return { patch, error: `${key}: numero non valido` };
        config[key] = Number(text);
      } else {
        config[key] = text;
      }
      dirty ||= text !== String(stored ?? "");
    } else {
      config[key] = stored; // nested values pass through (their masked leaves merge back)
    }
  }
  for (const key of emptyFields(connector)) {
    if (edits.fields[key]) {
      config[key] = edits.fields[key];
      dirty = true;
    }
  }
  if (dirty) patch.config = config;
  return { patch, error: null };
}

