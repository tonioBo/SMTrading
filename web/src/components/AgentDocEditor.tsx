import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { ApiError } from "@/api/client";
import { useSaveAgentDoc } from "@/api/hooks/agentDocs";
import type { AgentDocDetail } from "@/api/types";
import { FormField, selectClass } from "@/components/FormField";
import { Markdown } from "@/components/Markdown";
import { Button } from "@/components/ui/button";
import { errorField, errorMessage } from "@/lib/errors";
import { cn } from "@/lib/utils";

const NOTE_MAX = 200;
const BODY_MAX_BYTES = 200 * 1024;

interface Props {
  doc: AgentDocDetail;
  onDone: () => void;
  /** conflict: drop the draft and load the text saved by someone else */
  onReload: () => void;
}

/** «Modifica»: textarea with a preview toggle, an optional note, Salva / Annulla. */
export function AgentDocEditor({ doc, onDone, onReload }: Props) {
  // the text and version the edit started from: later refetches of `doc` do not change them
  const [body, setBody] = useState(doc.body);
  const [baseVersion] = useState(doc.current_version);
  const [note, setNote] = useState("");
  const [preview, setPreview] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = useSaveAgentDoc(doc.slug);
  const conflict = save.error instanceof ApiError && save.error.status === 409;
  const field = errorField(save.error);

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!body.trim()) return setError("Il testo non può essere vuoto");
    if (new TextEncoder().encode(body).length > BODY_MAX_BYTES) return setError("Il testo supera 200 kB");
    setError(null);
    save.mutate(
      { body, note: note.trim() || null, base_version: baseVersion },
      {
        onSuccess: ({ changed, doc: saved }) => {
          toast.success(changed ? `Salvata la versione ${saved.current_version}` : "Nessuna modifica da salvare");
          onDone();
        },
      },
    );
  }

  return (
    <form onSubmit={submit} className="space-y-3 p-4" aria-label={`Modifica ${doc.title}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted">Stai modificando la versione {baseVersion}.</p>
        <Button type="button" variant="outline" size="sm" aria-pressed={preview} onClick={() => setPreview((p) => !p)}>
          {preview ? "Torna al testo" : "Anteprima"}
        </Button>
      </div>
      {preview ? (
        <div className="min-h-[24rem] rounded-md border border-border p-3" aria-label="Anteprima">
          <Markdown source={body} />
        </div>
      ) : (
        <FormField label="Testo (Markdown)" error={error ?? (field === "body" ? errorMessage(save.error) : null)}>
          <textarea
            className={cn(selectClass, "h-[60vh] min-h-[24rem] py-2 font-mono text-xs leading-relaxed")}
            value={body}
            spellCheck={false}
            onChange={(e) => setBody(e.target.value)}
          />
        </FormField>
      )}
      <FormField
        label="Nota (facoltativa)"
        hint="Cosa è cambiato, compare nell'elenco delle versioni"
        error={field === "note" ? errorMessage(save.error) : null}
      >
        <input className={selectClass} value={note} maxLength={NOTE_MAX} onChange={(e) => setNote(e.target.value)} />
      </FormField>
      {conflict && (
        <div role="alert" className="flex flex-wrap items-center gap-3 rounded-md border border-warn p-3 text-sm">
          <span>{errorMessage(save.error)}</span>
          <Button type="button" variant="outline" size="sm" onClick={onReload}>
            Ricarica
          </Button>
          <span className="text-xs text-muted">Ricaricando perdi le modifiche non salvate: copiale prima se ti servono.</span>
        </div>
      )}
      {save.error && !conflict && field === null && (
        <p role="alert" className="text-sm text-loss">
          {errorMessage(save.error)}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={save.isPending}>
          Salva
        </Button>
        <Button type="button" variant="outline" onClick={onDone} disabled={save.isPending}>
          Annulla
        </Button>
      </div>
    </form>
  );
}
