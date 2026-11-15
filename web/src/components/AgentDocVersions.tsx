import { useState } from "react";
import { toast } from "sonner";
import { useAgentDocVersion, useAgentDocVersions, useRestoreAgentDoc } from "@/api/hooks/agentDocs";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Markdown } from "@/components/Markdown";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/errors";
import { formatBytes, formatDateTime } from "@/lib/format";
import { useIsAdmin } from "@/api/hooks/auth";

const author = (a: string) => (a === "seed" ? "testo iniziale" : a);

/** «Versioni»: every version of a document, newest first; view one, restore one. */
export function AgentDocVersions({ slug, currentVersion }: { slug: string; currentVersion: number }) {
  const admin = useIsAdmin();
  const { data: versions, isError } = useAgentDocVersions(slug, true);
  const [viewing, setViewing] = useState<number | null>(null);
  const [restoring, setRestoring] = useState<number | null>(null);
  const shown = useAgentDocVersion(slug, viewing);
  const restore = useRestoreAgentDoc(slug);

  function confirmRestore() {
    if (restoring === null) return;
    restore.mutate(restoring, {
      onSuccess: ({ doc }) => {
        toast.success(`Versione ${restoring} ripristinata come versione ${doc.current_version}`);
        setRestoring(null);
        setViewing(null);
      },
    });
  }

  return (
    <section aria-labelledby={`versions-${slug}`} className="border-t border-border p-4">
      <h3 id={`versions-${slug}`} className="mb-2 font-semibold">
        Versioni
      </h3>
      {isError && <p className="text-sm text-loss">Impossibile caricare le versioni.</p>}
      <ul className="divide-y divide-border rounded-md border border-border">
        {versions?.map((v) => (
          <li key={v.version} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
            <span className="font-medium">
              v{v.version}
              {v.version === currentVersion && <span className="ml-1 text-xs text-accent">(corrente)</span>}
            </span>
            <span className="text-muted">{formatDateTime(v.created_at)}</span>
            <span className="text-muted">{author(v.author)}</span>
            <span className="num text-xs text-muted">{formatBytes(v.size)}</span>
            {v.note && <span className="basis-full text-xs">{v.note}</span>}
            <span className="ml-auto flex gap-2">
              <Button
                variant="outline"
                size="sm"
                aria-pressed={viewing === v.version}
                aria-label={`Visualizza versione ${v.version}`}
                onClick={() => setViewing(viewing === v.version ? null : v.version)}
              >
                {viewing === v.version ? "Chiudi" : "Visualizza"}
              </Button>
              {v.version !== currentVersion && admin && (
                <Button
                  variant="outline"
                  size="sm"
                  aria-label={`Ripristina versione ${v.version}`}
                  onClick={() => {
                    restore.reset();
                    setRestoring(v.version);
                  }}
                >
                  Ripristina
                </Button>
              )}
            </span>
          </li>
        ))}
      </ul>
      {viewing !== null && (
        <div className="mt-3 rounded-md border border-border p-3" aria-label={`Testo della versione ${viewing}`}>
          {shown.data ? <Markdown source={shown.data.body} /> : <p className="text-sm text-muted">Caricamento…</p>}
        </div>
      )}
      <ConfirmDialog
        open={restoring !== null}
        onOpenChange={(open) => !open && setRestoring(null)}
        title={`Ripristinare la versione ${restoring}?`}
        description="Il testo di quella versione diventa una nuova versione corrente; nessuna versione viene cancellata."
        confirmLabel="Ripristina"
        pending={restore.isPending}
        error={restore.error ? errorMessage(restore.error) : null}
        onConfirm={confirmRestore}
      />
    </section>
  );
}
