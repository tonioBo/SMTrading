import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { RouterProvider, type createBrowserRouter } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import { providerFuture } from "@/routes";
import { TooltipProvider } from "@/components/ui/tooltip";

interface AppProps {
  queryClient: QueryClient;
  router: ReturnType<typeof createBrowserRouter>;
}

export function App({ queryClient, router }: AppProps) {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={300}>
        <RouterProvider router={router} future={providerFuture} />
        <Toaster richColors position="bottom-right" />
      </TooltipProvider>
    </QueryClientProvider>
  );
}
