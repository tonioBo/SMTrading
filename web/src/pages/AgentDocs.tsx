import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAgentDoc, useAgentDocExportStatus, useAgentDocs, useExportAgentDocs } from "@/api/hooks/agentDocs";
import type { AgentDocSummary } from "@/api/types";
import { AgentDocEditor } from "@/components/AgentDocEditor";
import { AgentDocVersions } from "@/components/AgentDocVersions";
import { Markdown } from "@/components/Markdown";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import {
  COMMON_SLUG,
  copyToClipboard,
  fullInstructions,
  isSetupDoc,
  rosterFor,
  type RosterEntry,
  type TagKind,
} from "@/lib/agentDocs";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const TAG_CLASS: Record<TagKind, string> = {
  plain: "border-border text-muted",
  trade: "border-warn text-warn",
  cron: "border-accent text-accent",
};

function Roster({
  entries,
  docs,
  selected,
  onSelect,
}: {
  entries: RosterEntry[];
  docs: Map<string, AgentDocSummary>;
  selected: string;
  onSelect: (slug: string) => void;
}) {
  const groups = [
    { id: "setup", title: "Preparazione", items: entries.filter((e) => e.section === "setup") },
    { id: "agents", title: "Agenti", items: entries.filter((e) => e.section === "agents") },
  ];
  return (
    <nav aria-label="Documenti" className="flex flex-col gap-4 lg:sticky lg:top-4">
      {groups.map((g) => (
        <div key={g.id}>
          <h2 className="mb-1.5 px-1 text-xs font-semibold uppercase tracking-wider text-muted">{g.title}</h2>
          <ul className="flex gap-2 overflow-x-auto pb-1 lg:flex-col lg:overflow-visible lg:pb-0">
            {g.items.map((e) => (
              <li key={e.slug} className="shrink-0 lg:shrink">
                <button
                  type="button"
                  aria-current={selected === e.slug ? "true" : undefined}
                  onClick={() => onSelect(e.slug)}
                  className={cn(
                    "grid w-44 gap-1 rounded-md border border-border bg-bg px-3 py-2 text-left text-sm transition-colors hover:bg-card focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent lg:w-full",
                    selected === e.slug && "border-accent bg-card",
                  )}
                >
                  <span className="font-medium">{e.label ?? docs.get(e.slug)?.title ?? e.slug}</span>
                  {e.tags.length > 0 && (
                    <span className="flex flex-wrap gap-1">
                      {e.tags.map((t) => (
                        <span
                          key={t.text}
                          className={cn("rounded-sm border px-1.5 py-0.5 font-mono text-[0.65rem] uppercase", TAG_CLASS[t.kind])}
                        >
                          {t.text}
                        </span>
                      ))}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}

const timeHM = (iso: string) =>
  new Date(iso).toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" });

function ExportStatus() {
  const status = useAgentDocExportStatus();
  const run = useExportAgentDocs();
  const s = status.data;
  let text = "Esportazione per Paperclip: in attesa del primo aggiornamento";
  let bad = false;
  if (s?.ok === false) {
    text = `Esportazione per Paperclip non riuscita: ${s.error ?? "errore sconosciuto"}`;
    bad = true;
  } else if (s?.ok && s.last_export_at) {
    text = `Esportazione per Paperclip: aggiornata alle ${timeHM(s.last_export_at)}`;
  }
  return (
    <div className="mb-4 flex flex-wrap items-center gap-3 text-sm">
      <p role="status" className={cn("text-xs", bad ? "text-loss" : "text-muted")}>
        {text}
      </p>
      <Button size="sm" variant="outline" disabled={run.isPending} onClick={() => run.mutate()}>
        Esporta ora
      </Button>
    </div>
  );
}

function PaperclipSettings({ slug, copy }: { slug: string; copy: (text: string, what: string) => void }) {
  const status = useAgentDocExportStatus();
  const root = status.data?.agents.find((a) => a.slug === slug)?.path ?? `/smtrading-instructions/${slug}`;
  const entry = status.data?.entry_file ?? "AGENTS.md";
  const rows = [
    { label: "Root path", value: root },
    { label: "Entry file", value: entry },
  ];
  return (
    <div className="mx-4 mt-3 rounded-md border border-border bg-card p-3 text-xs">
      <p className="mb-2 font-medium">Impostazioni «External» in Paperclip</p>
      <dl className="grid gap-1.5">
        {rows.map((r) => (
          <div key={r.label} className="flex flex-wrap items-center gap-2">
            <dt className="w-20 text-muted">{r.label}</dt>
            <dd>
              <code className="font-mono">{r.value}</code>
            </dd>
            <Button size="sm" variant="outline" aria-label={`Copia ${r.label}`} onClick={() => copy(r.value, r.label)}>
              Copia
            </Button>
          </div>
        ))}
      </dl>
    </div>
  );
}

function DocPane({ slug, title }: { slug: string; title: string }) {
  const setup = isSetupDoc(slug);
  const doc = useAgentDoc(slug);
  const common = useAgentDoc(COMMON_SLUG);
  const [editing, setEditing] = useState(false);
  const [editorKey, setEditorKey] = useState(0);
  const [showVersions, setShowVersions] = useState(false);
  const [status, setStatus] = useState("");
  const [fallback, setFallback] = useState<string | null>(null);
  const fallbackRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (fallback !== null) {
      fallbackRef.current?.focus();
      fallbackRef.current?.select();
    }
  }, [fallback]);

  async function copy(text: string, what: string) {
    if (await copyToClipboard(text)) {
      setFallback(null);
      setStatus(`${what} copiato negli appunti`);
    } else {
      setFallback(text);
      setStatus("Copia non consentita dal browser: il testo è selezionato qui sotto, premi Ctrl+C");
    }
  }

  const body = doc.data?.body;
  const commonBody = common.data?.body;

  return (
    <section aria-labelledby="doc-title" className="min-w-0 rounded-lg border border-border bg-bg">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border p-4">
        <div className="min-w-0">
          <h2 id="doc-title" className="text-lg font-semibold">
            {title}
          </h2>
          {doc.data && (
            <p className="text-xs text-muted">
              Versione {doc.data.current_version} · {formatDateTime(doc.data.updated_at)} ·{" "}
              {doc.data.updated_by === "seed" ? "testo iniziale" : doc.data.updated_by}
            </p>
          )}
        </div>
        {!editing && body !== undefined && (
          <div className="flex flex-wrap gap-2">
            {setup ? (
              <Button size="sm" onClick={() => void copy(body, "Testo")}>
                Copia testo
              </Button>
            ) : (
              <>
                <Button
                  size="sm"
                  disabled={commonBody === undefined}
                  onClick={() => commonBody !== undefined && void copy(fullInstructions(commonBody, body), "Istruzioni complete")}
                >
                  Copia istruzioni complete
                </Button>
                <Button size="sm" variant="outline" onClick={() => void copy(body, "Ruolo")}>
                  Copia solo ruolo
                </Button>
              </>
            )}
            <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
              Modifica
            </Button>
            <Button size="sm" variant="outline" aria-expanded={showVersions} onClick={() => setShowVersions((v) => !v)}>
              Versioni
            </Button>
          </div>
        )}
      </div>
      {!setup && !editing && (
        <p className="px-4 pt-3 text-xs text-muted">
          «Copia istruzioni complete» = regole comuni + ruolo: è il testo da incollare nelle istruzioni dell'agente in
          Paperclip.
        </p>
      )}
      {!setup && !editing && <PaperclipSettings slug={slug} copy={(t, w) => void copy(t, w)} />}
      <p role="status" aria-live="polite" className="min-h-[1.25rem] px-4 pt-2 text-xs text-accent">
        {status}
      </p>
      {fallback !== null && (
        <div className="px-4">
          <textarea
            ref={fallbackRef}
            readOnly
            aria-label="Testo da copiare"
            value={fallback}
            className="h-40 w-full rounded-md border border-border bg-card p-2 font-mono text-xs"
          />
        </div>
      )}
      {doc.isError && <p className="p-4 text-sm text-loss">Impossibile caricare il documento.</p>}
      {doc.data &&
        (editing ? (
          <AgentDocEditor
            key={editorKey}
            doc={doc.data}
            onDone={() => setEditing(false)}
            onReload={() => void doc.refetch().then(() => setEditorKey((k) => k + 1))}
          />
        ) : (
          <>
            {showVersions && <AgentDocVersions slug={slug} currentVersion={doc.data.current_version} />}
            <article className="px-4 pb-6 sm:px-6">
              <Markdown source={doc.data.body} />
            </article>
          </>
        ))}
    </section>
  );
}

/** «Istruzioni agenti»: read, copy, edit and version the Paperclip agent instructions. */
export default function AgentDocs() {
  const { data, isError } = useAgentDocs();
  const [params, setParams] = useSearchParams();
  const docs = new Map((data ?? []).map((d) => [d.slug, d]));
  const entries = rosterFor((data ?? []).map((d) => d.slug));
  const requested = params.get("doc");
  const selected = entries.find((e) => e.slug === requested) ?? entries[0];

  return (
    <div>
      <PageHeader title="Istruzioni agenti" />
      <p className="-mt-4 mb-6 text-sm text-muted">
        I 9 agenti Paperclip: le regole comuni vanno incollate in testa alle istruzioni di ogni agente.
      </p>
      {isError && <p className="text-sm text-loss">Impossibile caricare le istruzioni.</p>}
      <ExportStatus />
      {selected && (
        <div className="grid items-start gap-5 lg:grid-cols-[17rem_minmax(0,1fr)]">
          <Roster
            entries={entries}
            docs={docs}
            selected={selected.slug}
            onSelect={(slug) => setParams({ doc: slug }, { replace: true })}
          />
          <DocPane
            key={selected.slug}
            slug={selected.slug}
            title={selected.label ?? docs.get(selected.slug)?.title ?? selected.slug}
          />
        </div>
      )}
    </div>
  );
}
