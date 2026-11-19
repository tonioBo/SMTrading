/**
 * «Mappa dei settori» (Apache ECharts treemap, tree-shaken: treemap + tooltip + canvas):
 * the sectors sized by member count and coloured by their average day change, or the
 * members of the opened sector; a click opens a sector (or a member's sheet). The
 * breadcrumb is the page's (MarketOverview). Lazily loaded (components/charts/index.tsx).
 */
import { TreemapChart } from "echarts/charts";
import { TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useMemo, useRef, useState } from "react";
import { usePalette, useResize } from "@/components/charts/hooks";
import type { SectorTreemapProps } from "@/components/charts/types";
import { useIsNarrow } from "@/lib/media";
import { changeLabel, memberNodes, sectorNodes, tooltipHtml, type TreemapNode } from "@/lib/sectorMap";

echarts.use([TreemapChart, TooltipComponent, CanvasRenderer]);


export default function SectorTreemap({ snapshot, sector, onSector, onMember }: SectorTreemapProps) {
  const ref = useRef<HTMLDivElement>(null);
  const palette = usePalette();
  const narrow = useIsNarrow();
  const [chart, setChart] = useState<echarts.ECharts | null>(null);
  const nodes = useMemo(() => (sector ? memberNodes(sector.members, palette) : sectorNodes(snapshot.sectors, palette)), [sector, snapshot, palette]);
  const handlers = useRef({ onSector, onMember, sector });
  handlers.current = { onSector, onMember, sector };

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const c = echarts.init(el, null, { renderer: "canvas" });
    c.on("click", (params: { data?: unknown }) => {
      const n = params.data as TreemapNode | undefined;
      if (!n) return;
      if (handlers.current.sector) handlers.current.onMember(n.id);
      else handlers.current.onSector(n.id);
    });
    setChart(c);
    return () => {
      setChart(null);
      c.dispose();
    };
  }, []);
  useResize(ref, () => chart?.resize(), chart);

  useEffect(() => {
    if (!chart) return;
    chart.setOption(
      {
        tooltip: {
          confine: true,
          backgroundColor: palette.bg,
          borderColor: palette.border,
          textStyle: { color: palette.fg, fontSize: 12 },
          formatter: (p: { data?: unknown }) => (p.data ? tooltipHtml(p.data as TreemapNode) : ""),
        },
        series: [
          {
            type: "treemap",
            left: 0,
            top: 0,
            right: 0,
            bottom: 0,
            roam: false,
            nodeClick: false,
            breadcrumb: { show: false },
            squareRatio: narrow ? 0.9 : 1.4,
            itemStyle: { borderColor: palette.bg, borderWidth: 2, gapWidth: 2 },
            label: {
              show: true,
              overflow: "truncate",
              fontSize: narrow ? 11 : 12,
              formatter: (p: { data?: unknown }) => {
                const n = p.data as TreemapNode;
                return `{name|${n.name}}\n${changeLabel(n.change)}`;
              },
              rich: { name: { fontWeight: 600, fontSize: narrow ? 11 : 13, lineHeight: 18 } },
            },
            data: nodes.map((n) => ({ ...n, itemStyle: { color: n.color }, label: { color: n.text } })),
          },
        ],
      },
      { notMerge: true },
    );
  }, [chart, nodes, palette, narrow]);

  return <div ref={ref} className="w-full" style={{ height: narrow ? 320 : 400 }} role="img" aria-label={sector ? `Titoli del settore ${sector.sector} (mappa)` : "Mappa dei settori (grafico)"} data-testid="sector-treemap" />;
}
