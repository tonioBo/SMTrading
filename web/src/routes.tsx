import { Navigate, type RouteObject } from "react-router-dom";
import { Layout } from "@/components/Layout";
import { RouteError } from "@/components/RouteError";
import Agents from "@/pages/Agents";
import Brokers from "@/pages/Brokers";
import Connectors from "@/pages/Connectors";
import Dashboard from "@/pages/Dashboard";
import Fees from "@/pages/Fees";
import Login from "@/pages/Login";
import Markets from "@/pages/Markets";
import McpLog from "@/pages/McpLog";
import PortfolioDetail from "@/pages/PortfolioDetail";
import Portfolios from "@/pages/Portfolios";
import Risk from "@/pages/Risk";
import Settings from "@/pages/Settings";

export const routes: RouteObject[] = [
  { path: "/login", element: <Login />, errorElement: <RouteError /> },
  {
    path: "/",
    element: <Layout />,
    errorElement: <RouteError />,
    children: [
      {
        // errors of a page render inside the layout (sidebar and header stay usable)
        errorElement: <RouteError />,
        children: [
          { index: true, element: <Dashboard /> },
          { path: "portafogli", element: <Portfolios /> },
          { path: "portafogli/:id", element: <PortfolioDetail /> },
          { path: "agenti", element: <Agents /> },
          { path: "commissioni", element: <Fees /> },
          { path: "rischio", element: <Risk /> },
          { path: "connettori", element: <Connectors /> },
          { path: "broker", element: <Brokers /> },
          { path: "mercati", element: <Markets /> },
          { path: "log-mcp", element: <McpLog /> },
          { path: "impostazioni", element: <Settings /> },
          { path: "*", element: <Navigate to="/" replace /> },
        ],
      },
    ],
  },
];

/** Opt in to the React Router v7 behaviours (silences the v6 future-flag warnings). */
export const routerFuture = {
  v7_relativeSplatPath: true,
  v7_fetcherPersist: true,
  v7_normalizeFormMethod: true,
  v7_partialHydration: true,
  v7_skipActionErrorRevalidation: true,
} as const;
export const providerFuture = { v7_startTransition: true } as const;
