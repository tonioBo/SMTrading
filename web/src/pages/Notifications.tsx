import { useEffect, useId, useState } from "react";
import { toast } from "sonner";
import { useAgents } from "@/api/hooks/agents";
import { usePortfolios } from "@/api/hooks/portfolios";
import {
  useDecisions,
  useDetectChats,
  useOutbox,
  useRetryAllFailed,
  useRetryOutbox,
  useSaveTelegramConfig,
  useSendTestMessage,
  useSetTelegramPaused,
  useTelegram,
} from "@/api/hooks/telegram";
import type {
  Decision,
  DecisionFilters,
  DecisionKind,
  DecisionOutcome,
  OutboxItem,
  OutboxStatus,
  TelegramChat,
  TelegramConfigPatch,
  TelegramSettings,
} from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { FormField, selectClass } from "@/components/FormField";
import { PageHeader } from "@/components/PageHeader";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { errorField, errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { AdminFieldset, AdminOnly } from "@/components/AdminOnly";

const STATUS: Record<OutboxStatus, { label: string; variant: BadgeProps["variant"] }> = {
  pending: { label: "In coda", variant: "secondary" },
  sent: { label: "Inviato", variant: "default" },
  failed: { label: "Fallito", variant: "destructive" },
  paused: { label: "In pausa", variant: "warning" },
  skipped: { label: "Scartato", variant: "outline" },
};
const CATEGORY: Record<string, string> = {
  event: "Evento",
  digest: "Digest delle 07:00",
  daily: "Riepilogo giornaliero",
  weekly: "Riepilogo settimanale",
  agent: "Agente",
  test: "Prova",
  live: "Denaro reale",
};
const LEVEL: Record<string, string> = { info: "Info", warning: "Avviso", critical: "Critico", executed: "Eseguito" };
const DECISION_KIND: Record<DecisionKind, string> = {
  proposal: "Proposta",
  approval: "Approvazione",
  veto: "Veto",
  allocation: "Allocazione",
  strategy: "Strategia",
  risk_rule: "Regola di rischio",
  other: "Altro",
  lesson: "Lezione",
  team_rule: "Regola del team",
};
const DECISION_OUTCOME: Record<DecisionOutcome, string> = {
  approvata: "Approvata",
  respinta: "Respinta",
  sospesa: "Sospesa",
  modificata: "Modificata",
  informativa: "Informativa",
};
const TRUNCATE_AT = 120;

const botName = (s: TelegramSettings | undefined) => s?.bot_username ?? "nome_del_bot";

// --- Collegamento ----------------------------------------------------------------------

function ConnectionSection({ settings }: { settings: TelegramSettings }) {
  const save = useSaveTelegramConfig();
  const setEnabled = useSaveTelegramConfig();
  const detect = useDetectChats();
  const sendTest = useSendTestMessage();
  const setPaused = useSetTelegramPaused();
  const unreadable = settings.error !== null;
  const hasToken = settings.bot_token_masked !== null && !unreadable;
  const [replacing, setReplacing] = useState(false);
  const [token, setToken] = useState("");
  // null = untouched: the field follows the server (e.g. @username → id, supergroup migration)
  const [chatDraft, setChatDraft] = useState<string | null>(null);
  const chatId = chatDraft ?? settings.chat_id ?? "";
  const [localError, setLocalError] = useState<string | null>(null);
  const editingToken = replacing || !hasToken;

  const serverField = errorField(save.error);
  const serverMessage = save.error ? errorMessage(save.error) : null;
  const tokenError = localError ?? (serverField === "bot_token" ? serverMessage : null);
  const chatError = serverField === "chat_id" ? serverMessage : null;
  const otherError = save.error && serverField !== "bot_token" && serverField !== "chat_id" ? serverMessage : null;

  // the mutations' variables may hold the typed token: never keep them in the query client
  const { reset: resetSave } = save;
  const { reset: resetDetect } = detect;
  useEffect(
    () => () => {
      resetSave();
      resetDetect();
    },
    [resetSave, resetDetect],
  );

  function cancelReplace() {
    setReplacing(false);
    setToken("");
    setLocalError(null);
    resetSave();
    resetDetect();
  }

  function submit() {
    setLocalError(null);
    const body: TelegramConfigPatch = {};
    const cleanToken = token.replace(/\s+/g, "");
    if (cleanToken) body.bot_token = cleanToken;
    else if (unreadable) {
      setLocalError("Reinserisci il token del bot");
      return;
    }
    const cleanChat = chatId.trim();
    if (cleanChat !== (settings.chat_id ?? "")) body.chat_id = cleanChat;
    if (Object.keys(body).length === 0) {
      toast.info("Nessuna modifica da salvare");
      return;
    }
    save.mutate(body, {
      onSuccess: () => {
        cancelReplace();
        setChatDraft(null); // show the id as the server saved it
        toast.success("Impostazioni Telegram salvate");
      },
    });
  }

  function chooseChat(chat: TelegramChat) {
    setChatDraft(String(chat.id));
    resetDetect();
  }

  return (
    <Card role="region" aria-label="Collegamento">
      <CardHeader>
        <CardTitle>Collegamento</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {settings.error && (
          <p role="alert" className="rounded-md border border-loss p-2 text-loss">
            {settings.error}
          </p>
        )}
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-0 flex-1 basis-60">
              {editingToken ? (
                <FormField label="Token del bot" error={tokenError} hint="Il token fornito da @BotFather; non viene più mostrato dopo il salvataggio">
                  <Input type="password" autoComplete="off" spellCheck={false} value={token} onChange={(e) => setToken(e.target.value)} />
                </FormField>
              ) : (
                <FormField label="Token del bot" hint={settings.bot_username ? <span className="font-mono">@{settings.bot_username}</span> : undefined}>
                  <Input readOnly value={settings.bot_token_masked ?? ""} />
                </FormField>
              )}
            </div>
            {hasToken &&
              (replacing ? (
                <Button type="button" variant="outline" onClick={cancelReplace}>
                  Annulla
                </Button>
              ) : (
                <Button type="button" variant="outline" onClick={() => setReplacing(true)}>
                  Sostituisci token
                </Button>
              ))}
          </div>

          <FormField label="ID del gruppo" error={chatError} hint={settings.chat_title && chatId.trim() === settings.chat_id ? settings.chat_title : undefined}>
            <Input autoComplete="off" value={chatId} onChange={(e) => setChatDraft(e.target.value)} placeholder="-1001234567890" />
          </FormField>

          <div className="space-y-2">
            <Button type="button" variant="outline" disabled={detect.isPending} onClick={() => detect.mutate(token.replace(/\s+/g, "") || null)}>
              Rileva gruppi
            </Button>
            <p className="text-xs text-muted">
              Scrivi <code className="font-mono">/start@{botName(settings)}</code> nel gruppo, poi premi Rileva gruppi. L&apos;elenco mostra
              solo i gruppi degli ultimi messaggi ricevuti dal bot e non ancora letti: se non compare nulla, scrivi di nuovo il comando.
            </p>
            {detect.error && (
              <p role="alert" className="text-loss">
                {errorMessage(detect.error)}
              </p>
            )}
            {detect.data &&
              (detect.data.chats.length === 0 ? (
                <p role="status" className="text-muted">
                  Nessun gruppo trovato: scrivi il comando nel gruppo e riprova
                </p>
              ) : (
                <ul aria-label="Gruppi rilevati" className="divide-y divide-border rounded-md border border-border">
                  {detect.data.chats.map((chat) => (
                    <li key={String(chat.id)}>
                      <button
                        type="button"
                        onClick={() => chooseChat(chat)}
                        className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-left hover:bg-bg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                      >
                        <span className="font-medium">{chat.title ?? "Senza titolo"}</span>
                        <span className="num text-xs text-muted">{String(chat.id)}</span>
                        <span className="text-xs text-muted">{chat.type}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              ))}
          </div>

          {otherError && (
            <p role="alert" className="text-loss">
              {otherError}
            </p>
          )}
          <Button type="submit" disabled={save.isPending}>
            Salva
          </Button>
        </form>

        <div className="space-y-3 border-t border-border pt-4">
          <div className="flex items-center gap-3">
            <Switch
              id="telegram-enabled"
              aria-describedby="telegram-enabled-note"
              checked={settings.enabled}
              disabled={setEnabled.isPending || (!settings.configured && !settings.enabled)}
              onCheckedChange={(enabled) => setEnabled.mutate({ enabled })}
            />
            <Label htmlFor="telegram-enabled">Notifiche attive</Label>
          </div>
          <p id="telegram-enabled-note" className="text-xs text-muted">
            Disattivando, gli eventi di questo periodo non verranno inviati. Per sospendere temporaneamente usa Pausa: i messaggi restano in coda.
          </p>
          {!settings.configured && <p className="text-xs text-muted">Salva token e ID del gruppo per attivare le notifiche</p>}
          {setEnabled.error && (
            <p role="alert" className="text-loss">
              {errorMessage(setEnabled.error)}
            </p>
          )}
          <div>
            <div className="flex items-center gap-3">
              <Switch
                id="telegram-paused"
                aria-describedby="telegram-paused-note"
                checked={settings.paused}
                disabled={setPaused.isPending}
                onCheckedChange={(paused) => setPaused.mutate(paused)}
              />
              <Label htmlFor="telegram-paused">Metti in pausa gli invii</Label>
            </div>
            <p id="telegram-paused-note" className="mt-1 text-xs text-muted">
              I messaggi restano in coda e partono alla ripresa (quelli non critici in pausa da più di 24 ore vengono scartati)
            </p>
          </div>
          {setPaused.error && (
            <p role="alert" className="text-loss">
              {errorMessage(setPaused.error)}
            </p>
          )}
          <p className="text-xs text-muted">
            In coda: <span className="num">{settings.outbox.pending}</span> · In pausa: <span className="num">{settings.outbox.paused}</span> ·
            Falliti: <span className="num">{settings.outbox.failed}</span>
          </p>
        </div>

        <div className="space-y-2 border-t border-border pt-4">
          <Button type="button" variant="outline" disabled={sendTest.isPending || !settings.configured} onClick={() => sendTest.mutate()}>
            Invia messaggio di prova
          </Button>
          {sendTest.data && (
            <p role="status" className={sendTest.data.ok ? "text-gain" : "text-loss"}>
              {sendTest.data.ok ? "Messaggio di prova inviato" : `Invio non riuscito: ${sendTest.data.error ?? "errore sconosciuto"}`}
            </p>
          )}
          {sendTest.error && (
            <p role="alert" className="text-loss">
              {errorMessage(sendTest.error)}
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function GuideSection({ settings }: { settings: TelegramSettings | undefined }) {
  return (
    <Card>
      <details className="group">
        <summary className="cursor-pointer select-none rounded-lg p-6 font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
          Guida rapida
        </summary>
        <div className="space-y-3 px-6 pb-6 text-sm">
          <ol className="list-decimal space-y-1 pl-5">
            <li>
              Su Telegram apri <span className="font-mono">@BotFather</span>, invia <code className="font-mono">/newbot</code> e copia il token che ti
              restituisce.
            </li>
            <li>Aggiungi il bot al gruppo che deve ricevere le notifiche.</li>
            <li>
              Scrivi <code className="font-mono">/start@{botName(settings)}</code> nel gruppo.
            </li>
            <li>Incolla qui il token, premi «Rileva gruppi», scegli il gruppo e salva.</li>
            <li>Premi «Invia messaggio di prova» per controllare il collegamento.</li>
          </ol>
          <p className="text-muted">
            Ore notturne 23:00-07:00: arrivano subito, senza suono, solo gli eventi critici e i messaggi degli agenti; gli altri eventi vanno nel
            digest delle 07:00. Riepilogo giornaliero alle 18:00, settimanale il venerdì alle 18:30 (ora italiana).
          </p>
        </div>
      </details>
    </Card>
  );
}

// --- Ultimi messaggi -------------------------------------------------------------------

function TextCell({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  if (text.length <= TRUNCATE_AT) return <span className="whitespace-pre-wrap break-words">{text}</span>;
  return (
    <div className="max-w-md">
      <span className="whitespace-pre-wrap break-words">{open ? text : `${text.slice(0, TRUNCATE_AT)}…`}</span>
      <Button type="button" size="sm" variant="ghost" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {open ? "Riduci" : "Mostra tutto"}
      </Button>
    </div>
  );
}

function OutboxSection() {
  const [status, setStatus] = useState("");
  const { data, isLoading, isError } = useOutbox(status);
  const retry = useRetryOutbox();
  const retryAll = useRetryAllFailed();
  const failed = useTelegram().data?.outbox.failed ?? 0;
  const titleId = useId();
  const columns: Column<OutboxItem>[] = [
    { key: "ts", header: "Data", primary: true, cell: (m) => <span className="num whitespace-nowrap">{formatDateTime(m.created_at)}</span> },
    { key: "source", header: "Origine", cell: (m) => (m.source === "system" ? "Sistema" : (m.agent ?? "Agente")) },
    { key: "category", header: "Tipo", cell: (m) => CATEGORY[m.category] ?? m.category },
    { key: "level", header: "Livello", cell: (m) => LEVEL[m.level] ?? m.level },
    { key: "text", header: "Testo", mobileFull: true, cell: (m) => <TextCell text={m.text} /> },
    {
      key: "status",
      header: "Stato",
      cell: (m) => <Badge variant={STATUS[m.status]?.variant ?? "outline"}>{STATUS[m.status]?.label ?? m.status}</Badge>,
    },
    { key: "attempts", header: "Tentativi", align: "right", cell: (m) => <span className="num">{m.attempts}</span> },
    { key: "error", header: "Errore", cell: (m) => (m.last_error ? <span className="text-xs text-loss">{m.last_error}</span> : "—") },
    {
      key: "actions",
      header: <span className="sr-only">Azioni</span>,
      cell: (m) =>
        m.status === "failed" ? (
          <AdminOnly>
            <Button type="button" size="sm" variant="outline" disabled={retry.isPending} onClick={() => retry.mutate(m.id)}>
              Riprova
            </Button>
          </AdminOnly>
        ) : null,
    },
  ];
  return (
    <Card role="region" aria-labelledby={titleId}>
      <CardHeader className="flex-row flex-wrap items-end justify-between gap-3 space-y-0">
        <CardTitle id={titleId}>Ultimi messaggi</CardTitle>
        {failed > 0 && (
          <AdminOnly>
            <Button type="button" variant="outline" disabled={retryAll.isPending} onClick={() => retryAll.mutate()}>
              Riprova tutti i falliti
            </Button>
          </AdminOnly>
        )}
        <div className="w-44">
          <FormField label="Stato">
            <select className={selectClass} value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">Tutti</option>
              {(Object.keys(STATUS) as OutboxStatus[]).map((s) => (
                <option key={s} value={s}>
                  {STATUS[s].label}
                </option>
              ))}
            </select>
          </FormField>
        </div>
      </CardHeader>
      <CardContent>
        {isError && <p className="mb-2 text-loss">Impossibile caricare i messaggi</p>}
        {retry.error && (
          <p role="alert" className="mb-2 text-loss">
            {errorMessage(retry.error)}
          </p>
        )}
        {retryAll.data?.retried === 0 && (
          <p role="status" className="mb-2 text-muted-foreground">
            Nessun messaggio da riprovare
          </p>
        )}
        {retryAll.error && (
          <p role="alert" className="mb-2 text-loss">
            {errorMessage(retryAll.error)}
          </p>
        )}
        <DataTable aria-label="Ultimi messaggi" columns={columns} rows={data?.items} rowKey={(m) => String(m.id)} loading={isLoading} empty="Nessun messaggio" />
      </CardContent>
    </Card>
  );
}

// --- Decisioni -------------------------------------------------------------------------

const NO_FILTERS: DecisionFilters = { agent_id: "", portfolio_id: "", kind: "" };

export function DecisionsSection() {
  const [filters, setFilters] = useState(NO_FILTERS);
  const agents = useAgents();
  const portfolios = usePortfolios({ includeArchived: true });
  const titleId = useId();
  const { data, isLoading, isError, hasNextPage, fetchNextPage, isFetchingNextPage } = useDecisions(filters);
  const set = (field: keyof DecisionFilters) => (e: { target: { value: string } }) => setFilters((f) => ({ ...f, [field]: e.target.value }));
  const rows = data?.pages.flatMap((p) => p.items);
  const columns: Column<Decision>[] = [
    { key: "ts", header: "Data", primary: true, cell: (d) => <span className="num whitespace-nowrap">{formatDateTime(d.created_at)}</span> },
    { key: "agent", header: "Agente", cell: (d) => d.agent ?? "—" },
    { key: "portfolio", header: "Portafoglio", cell: (d) => d.portfolio ?? "—" },
    { key: "kind", header: "Tipo", cell: (d) => DECISION_KIND[d.kind] ?? d.kind },
    { key: "outcome", header: "Esito", cell: (d) => (d.outcome ? (DECISION_OUTCOME[d.outcome] ?? d.outcome) : "—") },
    { key: "subject", header: "Oggetto", mobileFull: true, cell: (d) => (d.subject ? <TextCell text={d.subject} /> : "—") },
    { key: "summary", header: "Sintesi", mobileFull: true, cell: (d) => <TextCell text={d.summary} /> },
    { key: "reason", header: "Motivo", mobileFull: true, cell: (d) => (d.reason ? <TextCell text={d.reason} /> : "—") },
    { key: "symbols", header: "Simboli", cell: (d) => (d.symbols.length ? <span className="font-mono text-xs">{d.symbols.join(", ")}</span> : "—") },
  ];
  return (
    <Card role="region" aria-labelledby={titleId}>
      <CardHeader>
        <CardTitle id={titleId}>Decisioni</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-3">
          <FormField label="Agente">
            <select className={selectClass} value={filters.agent_id} onChange={set("agent_id")}>
              <option value="">Tutti</option>
              {agents.data?.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </FormField>
          <FormField label="Portafoglio">
            <select className={selectClass} value={filters.portfolio_id} onChange={set("portfolio_id")}>
              <option value="">Tutti</option>
              {portfolios.data?.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </FormField>
          <FormField label="Tipo">
            <select className={selectClass} value={filters.kind} onChange={set("kind")}>
              <option value="">Tutti</option>
              {(Object.keys(DECISION_KIND) as DecisionKind[]).map((k) => (
                <option key={k} value={k}>
                  {DECISION_KIND[k]}
                </option>
              ))}
            </select>
          </FormField>
        </div>
        {isError && <p className="text-loss">Impossibile caricare le decisioni</p>}
        <DataTable aria-label="Decisioni" columns={columns} rows={rows} rowKey={(d) => String(d.id)} loading={isLoading} empty="Nessuna decisione" />
        {hasNextPage && (
          <Button type="button" variant="outline" disabled={isFetchingNextPage} onClick={() => void fetchNextPage()}>
            Carica altre
          </Button>
        )}
      </CardContent>
    </Card>
  );
}

export default function Notifications() {
  const { data, isLoading, isError } = useTelegram();
  return (
    <div className="space-y-6">
      <PageHeader title="Notifiche" />
      {isError && <p className="text-loss">Impossibile caricare le impostazioni di Telegram</p>}
      {isLoading && <p className="text-sm text-muted">Caricamento…</p>}
      {data && (
        <AdminFieldset>
          <ConnectionSection settings={data} />
        </AdminFieldset>
      )}
      <GuideSection settings={data} />
      <OutboxSection />
      <DecisionsSection />
    </div>
  );
}
