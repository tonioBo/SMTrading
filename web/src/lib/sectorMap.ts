/**
 * Data of the «Mappa dei settori» treemap (`components/charts/SectorTreemap`, ECharts):
 * one tile per sector sized by its member count (the snapshot has no market cap), or
 * one per member of the opened sector (equal sizes), coloured by the day change on a
 * diverging scale red → grey → green, full colour at ±3%.
 */
import type { Mover, SectorTile } from "@/api/marketsTypes";
import { rgbOf, type Palette } from "@/lib/chartTheme";
import { signedNumber } from "@/lib/lineChart";

/** ±FULL_SCALE % or more is the full colour. */
export const FULL_SCALE = 3;

export interface TreemapNode {
  /** the sector name, or the member's symbol */
  id: string;
  name: string;
  value: number;
  change: number | null;
  color: string;
  /** the label colour: dark on light tiles, white on dark ones */
  text: string;
  /** a sector's counts (none for a member) */
  count?: number;
  up?: number;
  down?: number;
  /** a member's full name */
  title?: string;
}

const mix = (a: [number, number, number], b: [number, number, number], t: number) =>
  a.map((x, i) => Math.round(x + (b[i] - x) * t)) as [number, number, number];

/** The neutral grey of an unchanged tile: halfway between the muted text and the card. */
function neutral(p: Palette): [number, number, number] {
  return mix(rgbOf(p.muted) ?? [128, 128, 128], rgbOf(p.card) ?? [40, 40, 40], 0.45);
}

/** A day change's colour: grey at 0, the theme's gain/loss colour from ±FULL_SCALE %. */
export function changeColor(pct: number | null, p: Palette): string {
  const base = neutral(p);
  if (pct == null || !Number.isFinite(pct) || pct === 0) return `rgb(${base.join(", ")})`;
  const end = rgbOf(pct > 0 ? p.gain : p.loss) ?? (pct > 0 ? [16, 185, 129] : [244, 63, 94]);
  const t = Math.min(Math.abs(pct) / FULL_SCALE, 1);
  return `rgb(${mix(base, end, t).join(", ")})`;
}

/** Readable label colour on `background` (relative luminance). */
export function textOn(background: string): string {
  const [r, g, b] = rgbOf(background) ?? [0, 0, 0];
  const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
  return lum > 0.6 ? "rgb(9, 9, 11)" : "rgb(250, 250, 250)";
}

const num = (v: string) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

/** The sector tiles (the snapshot order: by member count, then name). */
export function sectorNodes(sectors: SectorTile[], p: Palette): TreemapNode[] {
  return sectors
    .filter((s) => s.count > 0)
    .map((s) => {
      const change = num(s.avg_change_pct);
      const color = changeColor(change, p);
      return { id: s.sector, name: s.sector, value: s.count, change, color, text: textOn(color), count: s.count, up: s.up, down: s.down };
    });
}

/** The members of the opened sector, equal tiles (no market cap). */
export function memberNodes(members: Mover[], p: Palette): TreemapNode[] {
  return members.map((m) => {
    const change = num(m.change_pct);
    const color = changeColor(change, p);
    return { id: m.symbol, name: m.symbol, title: m.name, value: 1, change, color, text: textOn(color) };
  });
}

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

/** A tile's change: "+1,50%", "—" without one. */
export const changeLabel = (v: number | null | undefined) => (v == null ? "—" : `${signedNumber(v, 2)}%`);

/** The tooltip of a tile (HTML, escaped): name, change, counts (a sector) or full name. */
export function tooltipHtml(n: TreemapNode): string {
  if (n.count != null) {
    return `<b>${esc(n.name)}</b><br/>media ${changeLabel(n.change)}<br/>${n.count} titoli · ▲${n.up ?? 0} ▼${n.down ?? 0}`;
  }
  return `<b>${esc(n.name)}</b>${n.title ? ` · ${esc(n.title)}` : ""}<br/>${changeLabel(n.change)}`;
}
