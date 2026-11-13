import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "./index.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter } from "react-router-dom";
import { configureClient } from "@/api/client";
import { App } from "@/App";
import { createQueryClient } from "@/queryClient";
import { routerFuture, routes } from "@/routes";
import { initTheme } from "@/theme";

initTheme();
const queryClient = createQueryClient();
const router = createBrowserRouter(routes, { future: routerFuture });
// 401: client-side navigation to /login (keeps the app loaded), cached data dropped
configureClient({
  onUnauthorized: (url) => {
    queryClient.clear();
    void router.navigate(url);
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App queryClient={queryClient} router={router} />
  </StrictMode>,
);
