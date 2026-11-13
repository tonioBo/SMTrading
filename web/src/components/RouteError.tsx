import { RefreshCw } from "lucide-react";
import { isRouteErrorResponse, useRouteError } from "react-router-dom";
import { Button } from "@/components/ui/button";

/** Route-level error page (instead of React Router's raw one): usually a stale bundle. */
export function RouteError() {
  const error = useRouteError();
  const detail = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : null;
  return (
    <div role="alert" className="flex min-h-[60vh] flex-col items-center justify-center gap-4 p-6 text-center">
      <h1 className="text-lg font-semibold">Si è verificato un errore nella pagina</h1>
      <p className="max-w-md text-sm text-muted">
        Spesso succede dopo un aggiornamento dell'applicazione: ricarica la pagina per usare la versione nuova.
      </p>
      {detail && <p className="max-w-md break-words font-mono text-xs text-muted">{detail}</p>}
      <Button onClick={() => window.location.reload()}>
        <RefreshCw />
        Ricarica
      </Button>
    </div>
  );
}
