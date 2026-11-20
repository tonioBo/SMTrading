/**
 * Real `equity-history` responses (dashboard, 20/11/2026): three portfolios, «Portafoglio
 * Sfida» out of the totals, the weekly total goal and the «Sfida +4%» portfolio goal.
 * «6h» crashed the page («Value is undefined»): the chart was unmounted while loading.
 */
import type { EquityHistory } from "@/api/hooks/equityHistory";
import eh12h from "./eh_12h.json";
import eh1d from "./eh_1d.json";
import eh2h from "./eh_2h.json";
import eh6h from "./eh_6h.json";

export const REAL_HISTORY = { "2h": eh2h, "6h": eh6h, "12h": eh12h, "1d": eh1d } as unknown as Record<"2h" | "6h" | "12h" | "1d", EquityHistory>;
