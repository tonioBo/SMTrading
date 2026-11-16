import { useEffect, useState } from "react";
import { toast } from "sonner";
import { useAgents } from "@/api/hooks/agents";
import { useIsAdmin } from "@/api/hooks/auth";
import { usePaperclipConfig, usePaperclipStatus, useSavePaperclipConfig, useVerifyPaperclip } from "@/api/hooks/paperclip";
import type { PaperclipConfig, PaperclipConfigPatch, PaperclipStatus, PaperclipVerifyResult } from "@/api/types";
import { FormField, selectClass } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { errorField, errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { expiryText, hostChanged, isolationInactive } from "@/lib/paperclip";

/**
 * «Ultima lettura: …», the last reader error (also a successful read without isolation rules),
 * and whether the isolation alarm is active.
 */
function StatusLine({ status, isError }: { status: PaperclipStatus | undefined; isError: boolean }) {
  if (!status) {
    return isError ? (
      <p role="status" className="text-sm text-loss">
        Impossibile leggere lo stato
      </p>
    ) : null;
  }
  return (
    <div className="space-y-1 text-sm">
      <p role="status" className="text-muted">
        Ultima lettura: {status.last_ok_at ? formatDateTime(status.last_ok_at) : "mai"}
      </p>
      {status.last_error && <p className="text-loss">Errore di lettura: {status.last_error}</p>}
      {status.protection_error && <p className="text-loss">Task «PROTEGGI» non creato: {status.protection_error}</p>}
      {status.isolation === "ok" && (
        <p className="text-muted">
          Regole di isolamento: {status.baseline_count} attese{status.baseline_at ? ` (dal ${formatDateTime(status.baseline_at)})` : ""}
        </p>
      )}
      {isolationInactive(status) && <p className="text-warn">Regole di isolamento non verificate: l&apos;allarme non è attivo</p>}
      {status.rebaseline_reason === "config" && (
        <p className="text-muted">Configurazione cambiata: nuova fotografia delle regole al prossimo controllo</p>
      )}
      {status.key_expiring && <p className="text-warn">{expiryText(status.key_expires_at, "La chiave board")}</p>}
    </div>
  );
}

/** The «Verifica» result: rules count and the Paperclip agents with their editable SMTrading match. */
function VerifyResult({
  result,
  mapping,
  onMap,
}: {
  result: PaperclipVerifyResult;
  mapping: Record<string, string>;
  onMap: (paperclipId: string, smtId: string) => void;
}) {
  const agents = useAgents().data ?? [];
  if (!result.ok) {
    return (
      <p role="alert" className="text-sm text-loss">
        Verifica non riuscita: {result.error}
      </p>
    );
  }
  return (
    <div className="space-y-2 text-sm">
      <p className="text-gain">Regole di isolamento lette: {result.policies_count}</p>
      <Table aria-label="Agenti Paperclip">
        <TableHeader>
          <TableRow>
            <TableHead>Agente Paperclip</TableHead>
            <TableHead>Agente SMTrading</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {result.agents.length === 0 ? (
            <TableRow>
              <TableCell colSpan={2} className="py-6 text-center text-muted">
                Nessun agente in Paperclip
              </TableCell>
            </TableRow>
          ) : (
            result.agents.map((a) => {
              const label = a.name ?? a.id;
              return (
                <TableRow key={a.id}>
                  <TableCell>
                    {label}
                    <span className="ml-2 font-mono text-xs text-muted">{a.id}</span>
                  </TableCell>
                  <TableCell>
                    <select
                      aria-label={`Agente SMTrading per ${label}`}
                      className={selectClass}
                      value={mapping[a.id] ?? a.smt_agent?.id ?? ""}
                      onChange={(e) => onMap(a.id, e.target.value)}
                    >
                      <option value="">— automatico (per nome) —</option>
                      {agents.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.name}
                        </option>
                      ))}
                    </select>
                  </TableCell>
                </TableRow>
              );
            })
          )}
        </TableBody>
      </Table>
      <p className="text-xs text-muted">Senza abbinamento esplicito vale lo stesso nome (maiuscole ignorate). Premi «Salva» per registrare gli abbinamenti.</p>
    </div>
  );
}

function EditForm({ config, status, statusError }: { config: PaperclipConfig; status: PaperclipStatus | undefined; statusError: boolean }) {
  const save = useSavePaperclipConfig();
  const verify = useVerifyPaperclip();
  const [baseUrl, setBaseUrl] = useState(config.base_url ?? "");
  const [companyId, setCompanyId] = useState(config.company_id ?? "");
  const [key, setKey] = useState("");
  const [enabled, setEnabled] = useState(config.enabled);
  // only the matches changed in the «Verifica» table ("" = back to the name match)
  const [mapDraft, setMapDraft] = useState<Record<string, string>>({});

  // the mutation's variables may hold the typed key: never keep them in the query client
  // (gcTime 0, reset on success and on error: the error is kept here, it never echoes the key)
  const [saveError, setSaveError] = useState<unknown>(null);
  const { reset: resetSave } = save;
  useEffect(() => () => resetSave(), [resetSave]);

  const field = errorField(saveError);
  const message = saveError ? errorMessage(saveError) : null;
  const fieldError = (name: string) => (field === name ? message : null);
  const otherError = saveError && !["base_url", "company_id", "board_key"].includes(field ?? "") ? message : null;

  function submit() {
    const patch: PaperclipConfigPatch = {};
    if (baseUrl.trim() !== (config.base_url ?? "")) patch.base_url = baseUrl.trim();
    if (companyId.trim() !== (config.company_id ?? "")) patch.company_id = companyId.trim();
    const cleanKey = key.replace(/\s+/g, "");
    if (cleanKey) patch.board_key = cleanKey;
    if (enabled !== config.enabled) patch.enabled = enabled;
    if (Object.keys(mapDraft).length > 0) {
      const map = { ...config.agent_map };
      for (const [pid, sid] of Object.entries(mapDraft)) {
        if (sid) map[pid] = sid;
        else delete map[pid];
      }
      patch.agent_map = map;
    }
    if (Object.keys(patch).length === 0) {
      toast.info("Nessuna modifica da salvare");
      return;
    }
    setSaveError(null);
    save.mutate(patch, {
      onSuccess: () => {
        setKey("");
        setMapDraft({});
        resetSave();
        verify.reset(); // its matches were computed with the previous configuration
        toast.success("Impostazioni Paperclip salvate");
      },
      onError: (err) => {
        setKey("");
        setSaveError(err);
        resetSave();
      },
    });
  }

  return (
    // not a <form>: the Radix switch inside a form needs ResizeObserver (its hidden input)
    <div className="space-y-4">
      <FormField label="Indirizzo" error={fieldError("base_url")}>
        <Input autoComplete="off" spellCheck={false} placeholder="https://paperclip.example.com" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
      </FormField>
      <FormField label="Id azienda" error={fieldError("company_id")}>
        <Input autoComplete="off" spellCheck={false} value={companyId} onChange={(e) => setCompanyId(e.target.value)} />
      </FormField>
      <FormField
        label="Chiave board"
        error={fieldError("board_key")}
        hint={
          config.board_key_masked
            ? hostChanged(config.base_url, baseUrl)
              ? "Indirizzo cambiato: reinserisci la chiave board (quella salvata non viene inviata al nuovo indirizzo)"
              : "Lascia vuoto per tenere la chiave salvata; non viene più mostrata dopo il salvataggio"
            : "Una chiave board pcp_board_… di sola lettura"
        }
      >
        <Input
          type="password"
          autoComplete="new-password"
          spellCheck={false}
          placeholder={config.board_key_masked ?? ""}
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
      </FormField>
      <label className="flex items-center gap-2 text-sm">
        <Switch checked={enabled} onCheckedChange={setEnabled} aria-label="Lettura attiva" />
        Lettura attiva
      </label>
      {otherError && (
        <p role="alert" className="text-sm text-loss">
          {otherError}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="button" disabled={save.isPending} onClick={submit}>
          Salva
        </Button>
        <Button type="button" variant="outline" disabled={verify.isPending} onClick={() => verify.mutate()}>
          Verifica
        </Button>
      </div>
      <p className="text-xs text-muted">«Verifica» legge Paperclip con la configurazione salvata.</p>
      {verify.error && (
        <p role="alert" className="text-sm text-loss">
          {errorMessage(verify.error)}
        </p>
      )}
      {verify.data && (
        <VerifyResult
          result={verify.data}
          mapping={{ ...config.agent_map, ...mapDraft }}
          onMap={(pid, sid) => setMapDraft((d) => ({ ...d, [pid]: sid }))}
        />
      )}
      <StatusLine status={status} isError={statusError} />
    </div>
  );
}

function ReadOnlyView({ config, status, statusError }: { config: PaperclipConfig; status: PaperclipStatus | undefined; statusError: boolean }) {
  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted">Indirizzo</dt>
        <dd className="break-all">{config.base_url ?? "—"}</dd>
        <dt className="text-muted">Id azienda</dt>
        <dd className="break-all">{config.company_id ?? "—"}</dd>
        <dt className="text-muted">Chiave board</dt>
        <dd className="font-mono">{config.board_key_masked ?? "—"}</dd>
        <dt className="text-muted">Lettura attiva</dt>
        <dd>{config.enabled ? "Sì" : "No"}</dd>
      </dl>
      <StatusLine status={status} isError={statusError} />
    </div>
  );
}

/** «Impostazioni → Paperclip»: the read-only connection to Paperclip (costs, isolation rules). */
export function PaperclipSection() {
  const admin = useIsAdmin();
  const config = usePaperclipConfig();
  const status = usePaperclipStatus();
  return (
    <Card className="mt-6" role="region" aria-label="Paperclip">
      <CardHeader>
        <CardTitle>Paperclip</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p className="text-muted">SMTrading legge da Paperclip, solo in lettura, i costi degli agenti e le regole di isolamento.</p>
        {config.isError && <p className="text-loss">Impossibile caricare la configurazione di Paperclip</p>}
        {config.data?.error && (
          <p role="alert" className="rounded-md border border-loss p-2 text-loss">
            {config.data.error}
          </p>
        )}
        {config.data && (admin ? <EditForm config={config.data} status={status.data} statusError={status.isError} /> : <ReadOnlyView config={config.data} status={status.data} statusError={status.isError} />)}
      </CardContent>
    </Card>
  );
}
