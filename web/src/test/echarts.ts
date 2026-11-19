/**
 * A stand-in for `echarts/core` in jsdom (no canvas): registered for every test in
 * setup.ts. It records the options set and lets a test click a node.
 */
type Handler = (params: { data?: unknown }) => void;

export class FakeECharts {
  option: Record<string, unknown> | null = null;
  handlers = new Map<string, Handler>();
  disposed = false;
  constructor(public el: HTMLElement) {}
  setOption(o: Record<string, unknown>) {
    this.option = o;
  }
  on(event: string, h: Handler) {
    this.handlers.set(event, h);
  }
  off(event: string) {
    this.handlers.delete(event);
  }
  resize() {}
  dispose() {
    this.disposed = true;
  }
  /** test helper: a click on the node with `data` */
  click(data: unknown) {
    this.handlers.get("click")?.({ data });
  }
}

export const instances: FakeECharts[] = [];

export function init(el: HTMLElement) {
  const c = new FakeECharts(el);
  instances.push(c);
  return c;
}

export function use() {}
