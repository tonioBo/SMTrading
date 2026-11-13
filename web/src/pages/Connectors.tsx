import { useState } from "react";
import { toast } from "sonner";
import { usePatchConnector, usePriorities, useStreamStatus, useTestConnector, useConnectors } from "@/api/hooks/connectors";
import type { AssetClass, Connector, StreamStatus } from "@/api/types";
import { FormField } from "@/components/FormField";
import { PageHeader } from "@/components/PageHeader";
import { PriorityCell } from "@/components/PriorityMatrix";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { errorMessage } from "@/lib/errors";
import { buildConnectorPatch, isScalar, isSecret } from "@/lib/connectorPatch";
import { formatDateTime } from "@/lib/format";

const ASSET_CLASS_LABELS: Record<AssetClass, string> = {
  us_equity: "Azioni USA",
  eu_equity: "Azioni UE",
  crypto: "Crypto",
  forex: "Forex",
};
const HEALTH_LABELS: Record<string, string> = { ok: "Operativo", error: "Errore", unknown: "Non verificato" };

function ConnectorCard({ connector }: { connector: Connector }) {
  const patch = usePatchConnector();
  const test = useTestConnector();
  const [rate, setRate] = useState(String(connector.rate_limit_per_min ?? ""));
  const [fields, setFields] = useState<Record<string, string>>({});
  const [removed, setRemoved] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const keys = Object.keys(connector.config_masked).filter((k) => isScalar(connector.config_masked[k]));
  if (connector.requires_key && !keys.includes("api_key") && !("api_key" in connector.config_masked)) keys.push("api_key");

  function save() {
    const { patch: body, error: err } = buildConnectorPatch(connector, { rate, fields, removed });
    setError(err);
    if (err || Object.keys(body).length === 0) return;
    patch.mutate(
      { name: connector.name, patch: body },
      {
        onSuccess: () => {
          setFields({});
          setRemoved(new Set());
          toast.success(`Connettore ${connector.name} salvato`);
        },
      },
    );
  }

  return (
    <Card aria-label={`Connettore ${connector.name}`}>
      <CardHeader className="flex-row items-start justify-between gap-2 space-y-0">
        <div>
          <CardTitle className="text-base">{connector.name}</CardTitle>
          <p className="mt-1 text-xs text-muted">{connector.capabilities.join(", ") || "—"}</p>
        </div>
        <div className="flex items-center gap-2">
          {!connector.implemented && <Badge variant="outline">Disponibile prossimamente</Badge>}
          <Switch
            aria-label={`Attiva ${connector.name}`}
            checked={connector.enabled}
            disabled={!connector.implemented || patch.isPending}
            onCheckedChange={(enabled) => patch.mutate({ name: connector.name, patch: { enabled } })}
          />
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge variant={connector.health_status === "ok" ? "default" : connector.health_status === "error" ? "destructive" : "secondary"}>
            {HEALTH_LABELS[connector.health_status] ?? connector.health_status}
          </Badge>
          {connector.last_checked_at && <span className="text-xs text-muted">verificato il {formatDateTime(connector.last_checked_at)}</span>}
        </div>
        {connector.last_error && <p className="text-xs text-loss">Ultimo errore: {connector.last_error}</p>}

        {connector.implemented && (
          <>
            {keys.map((key) => {
              const stored = connector.config_masked[key];
              const secret = isSecret(key);
              const label = key;
              return (
                <div key={key} className="flex items-end gap-2">
                  <div className="flex-1">
                    <FormField label={label}>
                      <Input
                        type={secret ? "password" : "text"}
                        autoComplete="off"
                        value={fields[key] ?? (secret ? "" : String(stored ?? ""))}
                        placeholder={secret ? (removed.has(key) ? "(verrà rimosso)" : String(stored ?? "")) : undefined}
                        disabled={removed.has(key)}
                        onChange={(e) => setFields((f) => ({ ...f, [key]: e.target.value }))}
                      />
                    </FormField>
                  </div>
                  {secret && stored !== null && stored !== undefined && stored !== "" && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      aria-label={`Rimuovi ${key}`}
                      onClick={() =>
                        setRemoved((r) => {
                          const next = new Set(r);
                          if (next.has(key)) next.delete(key);
                          else next.add(key);
                          return next;
                        })
                      }
                    >
                      {removed.has(key) ? "Annulla rimozione" : "Rimuovi"}
                    </Button>
                  )}
                </div>
              );
            })}
            <FormField label="Limite richieste al minuto">
              <Input value={rate} inputMode="numeric" onChange={(e) => setRate(e.target.value)} />
            </FormField>
          </>
        )}

        {(error ?? (patch.error ? errorMessage(patch.error) : null)) && (
          <p role="alert" className="text-sm text-loss">
            {error ?? errorMessage(patch.error)}
          </p>
        )}
        {test.data && (
          <p role="status" className={test.data.ok ? "text-sm text-gain" : "text-sm text-loss"}>
            {test.data.ok ? "Connessione riuscita" : `Test fallito: ${test.data.error ?? "errore sconosciuto"}`}
          </p>
        )}
        {test.error && (
          <p role="alert" className="text-sm text-loss">
            {errorMessage(test.error)}
          </p>
        )}
        {connector.implemented && (
          <div className="flex gap-2">
            <Button type="button" size="sm" onClick={save} disabled={patch.isPending}>
              Salva
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={() => test.mutate(connector.name)} disabled={test.isPending}>
              Testa connessione
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

const STATE_VARIANT: Record<string, "default" | "warning" | "destructive"> = {
  connected: "default",
  connecting: "warning",
  backoff: "destructive",
};
const STATE_LABEL: Record<string, string> = { connected: "Connesso", connecting: "In connessione", backoff: "In attesa di riconnessione" };

function StreamSection({ status }: { status: StreamStatus | undefined }) {
  const streams = Object.entries(status?.streams ?? {});
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Stato dello streaming</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {status && !status.available && <p className="text-warn">Stato dello streaming non disponibile (worker fermo?)</p>}
        <p>
          Simboli seguiti: <span className="num">{status?.tracked.length ?? 0}</span>
          {status?.tracked.length ? ` (${status.tracked.join(", ")})` : ""}
        </p>
        {streams.length === 0 ? (
          <p className="text-muted">Nessuno stream attivo</p>
        ) : (
          <ul className="space-y-1">
            {streams.map(([key, s]) => (
              <li key={key} className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{key}</span>
                <Badge variant={STATE_VARIANT[s.state] ?? "secondary"}>{STATE_LABEL[s.state] ?? s.state}</Badge>
                <span className="text-xs text-muted">{s.symbols.join(", ")}</span>
              </li>
            ))}
          </ul>
        )}
        <p>In polling: {status?.polling.length ? status.polling.join(", ") : "nessuno"}</p>
      </CardContent>
    </Card>
  );
}

function PrioritySection() {
  const { data, isError } = usePriorities();
  const cells = (data ?? []).filter((p) => p.providers.length > 0);
  const capabilities = [...new Set(cells.map((p) => p.capability))];
  const classes = [...new Set(cells.map((p) => p.asset_class))];
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Priorità dei provider</CardTitle>
        <p className="text-sm text-muted">Trascina (o usa le frecce) per cambiare l&apos;ordine di preferenza; poi salva la cella.</p>
      </CardHeader>
      <CardContent>
        {isError && <p className="text-loss">Impossibile caricare le priorità</p>}
        <div className="space-y-6">
          {classes.map((ac) => (
            <section key={ac} aria-label={ASSET_CLASS_LABELS[ac] ?? ac}>
              <h3 className="mb-2 font-medium">{ASSET_CLASS_LABELS[ac] ?? ac}</h3>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {capabilities.map((cap) => {
                  const cell = cells.find((p) => p.asset_class === ac && p.capability === cap);
                  return cell ? (
                    <div key={cap}>
                      <p className="mb-1 text-xs uppercase text-muted">{cap}</p>
                      <PriorityCell priority={cell} />
                    </div>
                  ) : null;
                })}
              </div>
            </section>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

export default function Connectors() {
  const { data, isLoading, isError } = useConnectors();
  const stream = useStreamStatus();
  return (
    <div>
      <PageHeader title="Connettori" />
      {isError && <p className="text-loss">Impossibile caricare i connettori</p>}
      {isLoading && <p className="text-muted">Caricamento…</p>}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {data?.map((c) => (
          <ConnectorCard key={c.name} connector={c} />
        ))}
      </div>
      <PrioritySection />
      <StreamSection status={stream.data} />
    </div>
  );
}
