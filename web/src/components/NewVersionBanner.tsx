import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useNewVersionAvailable } from "@/lib/buildVersion";

/** Shown once the server reports a build other than this bundle's. */
export function NewVersionBanner() {
  const outdated = useNewVersionAvailable();
  if (!outdated) return null;
  return (
    <div role="alert" className="flex flex-wrap items-center justify-center gap-3 border-b border-accent/40 bg-accent/10 px-4 py-1.5 text-sm">
      <span>Nuova versione disponibile — Ricarica</span>
      <Button size="sm" variant="outline" onClick={() => window.location.reload()}>
        <RefreshCw />
        Ricarica
      </Button>
    </div>
  );
}
