import { http, HttpResponse } from "msw";

/** Requests every authenticated page makes (Layout, halt banners): mocked once for all page tests. */
export const commonHandlers = [
  http.get("*/api/dashboard/modes", () => HttpResponse.json({ simulazione: 1, paper: 0, reale: 0 })),
  http.get("*/api/halts", () => HttpResponse.json({ active: [], history: [] })),
];
