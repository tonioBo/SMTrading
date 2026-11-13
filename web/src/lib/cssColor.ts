/** A `--token: r g b` CSS variable as "rgb(r, g, b)" (lightweight-charts rejects the space syntax). */
export function cssColor(name: string, fallback: string): string {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return raw ? `rgb(${raw.split(/\s+/).join(", ")})` : fallback;
}
