import { vi } from "vitest";
import "@testing-library/jest-dom/vitest";

// jsdom's AbortSignal is not the one Node's fetch `Request` accepts (react-router passes
// one on every navigation): drop the signal in tests, navigation is not aborted anyway.
const NodeRequest = globalThis.Request;
globalThis.Request = class extends NodeRequest {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    if (init?.signal) {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      const { signal, ...rest } = init;
      super(input, rest);
    } else {
      super(input, init);
    }
  }
} as typeof Request;

// The canvas charts: no canvas in jsdom, so stand-ins that record what is drawn
// (components/charts/: Lightweight Charts and the ECharts treemap).
vi.mock("lightweight-charts", () => import("./lightweightCharts"));
vi.mock("echarts/core", () => import("./echarts"));
vi.mock("echarts/charts", () => ({ TreemapChart: {} }));
vi.mock("echarts/components", () => ({ TooltipComponent: {} }));
vi.mock("echarts/renderers", () => ({ CanvasRenderer: {} }));
