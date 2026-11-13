import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { createQueryClient } from "@/queryClient";
import { providerFuture, routerFuture, routes } from "@/routes";

export function renderApp(initialPath: string) {
  const queryClient = createQueryClient();
  const router = createMemoryRouter(routes, { initialEntries: [initialPath], future: routerFuture });
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} future={providerFuture} />
    </QueryClientProvider>,
  );
  return { ...utils, router, queryClient };
}
