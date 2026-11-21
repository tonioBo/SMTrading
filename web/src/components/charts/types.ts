/** Props of the lazily loaded charts (kept apart so pages import no chart library). */
import type { MarketSnapshot, SectorTile } from "@/api/marketsTypes";
import type { TooltipRow } from "@/lib/equityHistory";
import type { ChartLine } from "@/lib/lineChart";
import type { Candle, PriceLine, PriceMarker, PricePoint } from "@/lib/priceChart";

export interface EquityChartProps {
  lines: ChartLine[];
  /** the portfolio detail: one portfolio, a stronger gradient */
  single: boolean;
  /** the price axis labels ("1.234,00 €", "+2,00%") */
  formatValue: (v: number) => string;
  /** the tooltip rows at a moment (epoch ms) */
  rowsAt: (t: number) => TooltipRow[];
  /** daily axis labels (dates) rather than hours */
  daily: boolean;
  ariaLabel: string;
  /** the zoom is reset when it changes (range, unit), kept on a refresh */
  resetKey: string;
}

export interface PriceOverlay {
  key: string;
  label: string;
  color: string;
  data: PricePoint[];
}

export interface PriceChartProps {
  candles: Candle[];
  timeframe: "1d" | "1h";
  /** a volume histogram in a pane below (when the bars have volumes) */
  volume?: boolean;
  /** lines over the candles (the moving averages) */
  overlays?: PriceOverlay[];
  /** our stop, target, entry… as labelled price lines (kept in view) */
  lines?: PriceLine[];
  /** our executions */
  markers?: PriceMarker[];
  ariaLabel: string;
  /** the price labels: the instrument's decimals and currency */
  formatPrice?: (v: number) => string;
}

export interface SectorTreemapProps {
  snapshot: MarketSnapshot;
  /** the opened sector (its members), or null for every sector */
  sector: SectorTile | null;
  onSector: (sector: string) => void;
  onMember: (symbol: string) => void;
}

export interface OverviewChartProps {
  /** [epoch seconds, value] */
  points: [number, number][];
  /** daily axis labels (dates) rather than hours */
  daily: boolean;
  formatValue: (v: number) => string;
  ariaLabel: string;
}
