import { RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useSystemHealth } from "@/api/hooks/system";
import type { HealthCycleRoutine, HealthJob, HealthProvider, HealthQuoteClass, HealthSectionBase, HealthSentinel, SystemHealth as SystemHealthData } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { PageHeader } from "@/components/PageHeader";
import { QuotaBars } from "@/components/QuotaBars";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatBytes, formatDateTime } from "@/lib/format";
import { ASSET_CLASS_LABELS, HEALTH_LABEL, HEALTH_TONE, JOB_LABELS, formatAge } from "@/lib/health";
import { formatUsd } from "@/lib/paperclip";
import { cn } from "@/lib/utils";

function Status({ status }: { status: HealthSectionBase["status"] }) {
  return <StatusBadge tone={HEALTH_TONE[status] ?? "neutral"}>{HEALTH_LABEL[status] ?? status}</StatusBadge>;
}

const when = (iso: string | null | undefined) => (iso ? formatDateTime(iso) : "—");

function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** One section: its status, the summary, the reasons, then its details. */
function Section({ id, section, children, link }: { id: string; section: HealthSectionBase; children?: ReactNode; link?: { to: string; label: string } }) {
  return (
    <Card role="region" aria-label={section.label} id={id} className={cn(section.status === "problema" && "border-loss/60", section.status === "attenzione" && "border-warn/60")}>
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2 space-y-0 pb-2">
        <CardTitle className="text-base">{section.label}</CardTitle>
        <div className="flex items-center gap-2">
          {link && (
            <Link to={link.to} className="text-sm text-accent hover:underline">
              {link.label}
            </Link>
          )}
          <Status status={section.status} />
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm">{section.summary}</p>
        {section.reasons.length > 0 && section.status !== "ok" && (
          <ul className="list-disc space-y-0.5 pl-5 text-sm text-muted" aria-label={`Motivi: ${section.label}`}>
            {section.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        )}
        {children}
      </CardContent>
    </Card>
  );
}

const jobColumns: Column<HealthJob>[] = [
  {
    key: "name",
    header: "Lavoro",
    primary: true,
    sortValue: (j) => JOB_LABELS[j.name] ?? j.name,
    cell: (j) => (
      <span className="flex flex-wrap items-center gap-2">
        <span title={j.name}>{JOB_LABELS[j.name] ?? j.name}</span>
        {j.status !== "ok" && <Status status={j.status} />}
      </span>
    ),
  },
  { key: "tick", header: "Ultimo giro", cell: (j) => (j.last_tick_at ? when(j.last_tick_at) : j.interval_s ? "—" : <span className="text-muted">continuo</span>) },
  { key: "started", header: "Avviato", cell: (j) => when(j.started_at) },
  { key: "crashes", header: "Errori", align: "right", sortValue: (j) => j.crashes, cell: (j) => <span className="num">{j.crashes}</span> },
  {
    key: "error",
    header: "Ultimo errore",
    cell: (j) =>
      j.last_error ? (
        <span className="text-xs" title={j.last_error}>
          {j.last_crash_at ? `${when(j.last_crash_at)} · ` : ""}
          {j.last_error}
        </span>
      ) : (
        <span className="text-muted">—</span>
      ),
  },
];

function Services({ s }: { s: SystemHealthData["sections"]["services"] }) {
  return (
    <Section id="servizi" section={s}>
      <Facts items={[["Battito del worker", s.heartbeat_age_s == null ? "nessuno" : `${formatAge(s.heartbeat_age_s)} fa`]]} />
      {s.jobs && s.jobs.length > 0 && <DataTable aria-label="Lavori del worker" columns={jobColumns} rows={s.jobs} rowKey={(j) => j.name} />}
    </Section>
  );
}

function Providers({ s }: { s: SystemHealthData["sections"]["providers"] }) {
  const rows: HealthProvider[] = s.providers ?? [];
  return (
    <Section id="fornitori" section={s} link={{ to: "/connettori", label: "Connettori" }}>
      {rows.length > 0 && (
        <ul className="space-y-1 text-sm" aria-label="Fornitori di dati">
          {rows.map((p) => (
            <li key={p.name} className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">{p.name}</span>
              <span className="flex flex-wrap items-center gap-2 text-muted">
                {p.last_error && <span className="text-xs text-loss">{p.last_error}</span>}
                <span className="text-xs">{p.last_checked_at ? `controllato ${when(p.last_checked_at)}` : "mai controllato"}</span>
                <Status status={p.status} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

const quoteColumns: Column<HealthQuoteClass>[] = [
  { key: "class", header: "Classe", primary: true, cell: (r) => ASSET_CLASS_LABELS[r.asset_class] ?? r.asset_class },
  { key: "symbols", header: "Simboli", align: "right", cell: (r) => <span className="num">{r.symbols}</span> },
  { key: "fresh", header: "Aggiornate", align: "right", cell: (r) => <span className="num">{r.fresh}</span> },
  {
    key: "stale",
    header: "Vecchie",
    align: "right",
    cell: (r) => (
      <span className={cn("num", r.stale > 0 && "text-warn")} title={r.stale_symbols.join(", ")}>
        {r.stale}
      </span>
    ),
  },
  { key: "closed", header: "Mercato chiuso", align: "right", cell: (r) => <span className="num">{r.closed}</span> },
  { key: "threshold", header: "Soglia", align: "right", cell: (r) => formatAge(r.threshold_s) },
];

function Quotes({ s }: { s: SystemHealthData["sections"]["quotes"] }) {
  return (
    <Section id="quotazioni" section={s}>
      {s.classes && s.classes.length > 0 && <DataTable aria-label="Quotazioni per classe" columns={quoteColumns} rows={s.classes} rowKey={(r) => r.asset_class} />}
      <p className="text-xs text-muted">Simboli delle posizioni aperte e delle liste; a mercato chiuso una quotazione vecchia è normale e non conta.</p>
    </Section>
  );
}

function PriceQuality({ s }: { s: NonNullable<SystemHealthData["sections"]["price_quality"]> }) {
  const current = s.current ?? [];
  const top = s.top_symbols ?? [];
  const reasons = s.by_reason ?? [];
  return (
    <Section id="qualita-prezzi" section={s}>
      <Facts
        items={[
          ["Sospette nell'ultima ora", <span key="h" className="num">{s.suspects_1h ?? 0}</span>],
          ["Sospette nelle 24 ore", <span key="d" className="num">{s.suspects_24h ?? 0}</span>],
          ["Da verificare ora", current.length ? current.map((c) => `${c.symbol} (da ${formatAge(c.age_s)})`).join(", ") : "nessuno"],
          ["Simboli più colpiti", top.length ? top.map((t) => `${t.symbol} ${t.count}`).join(", ") : "—"],
        ]}
      />
      {reasons.length > 0 && (
        <ul className="flex flex-wrap gap-2 text-xs" aria-label="Classificazioni nelle 24 ore">
          {reasons.map((r) => (
            <li key={r.reason} className="rounded-md border border-border px-2 py-0.5">
              {r.label} <span className="num">{r.count}</span>
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs text-muted">
        Un prezzo con un salto improvviso o in disaccordo tra le fonti viene confrontato con le altre fonti; se nessuna lo conferma è «sospetto»: ordini e stop aspettano
        una quotazione verificata. Con una sola fonte, un salto che resta tale per 2 minuti (o dopo 30 minuti dall&apos;ultimo prezzo accettato) diventa il nuovo prezzo.
      </p>
    </Section>
  );
}

function Quotas({ s }: { s: NonNullable<SystemHealthData["sections"]["quotas"]> }) {
  const rows = (s.providers ?? []).filter((p) => p.quota.per_hour || p.quota.per_day || p.quota.per_min);
  return (
    <Section id="quote-fonti" section={s} link={{ to: "/connettori", label: "Connettori" }}>
      {rows.length > 0 && (
        <ul className="space-y-3" aria-label="Quota per fornitore">
          {rows.map((p) => (
            <li key={p.name} className="space-y-1">
              <span className="text-sm font-medium">{p.name}</span>
              <QuotaBars name={p.name} quota={p.quota} />
            </li>
          ))}
        </ul>
      )}
      {s.note && <p className="text-xs text-muted">Quote {s.note}.</p>}
    </Section>
  );
}

function Paperclip({ s }: { s: SystemHealthData["sections"]["paperclip"] }) {
  const r = s.reader;
  return (
    <Section id="paperclip" section={s} link={{ to: "/impostazioni", label: "Impostazioni" }}>
      {r?.enabled && (
        <Facts
          items={[
            ["Ultima lettura riuscita", when(r.last_ok_at)],
            ["Errori consecutivi", <span key="e" className="num">{r.consecutive_errors ?? 0}</span>],
            ["Ultimo errore", r.last_error ?? "—"],
          ]}
        />
      )}
    </Section>
  );
}

const cycleColumns: Column<HealthCycleRoutine>[] = [
  {
    key: "routine",
    header: "Routine",
    primary: true,
    cell: (r) => (
      <span>
        {r.routine ?? "—"}
        {r.agent && <span className="ml-2 text-xs text-muted">{r.agent}</span>}
      </span>
    ),
  },
  { key: "expected", header: "Attesi finora", align: "right", cell: (r) => <span className="num">{r.expected}</span> },
  {
    key: "done",
    header: "Partiti",
    align: "right",
    cell: (r) => <span className={cn("num", r.done < r.expected && "text-warn")}>{r.done}</span>,
  },
  { key: "completed", header: "Completati", align: "right", cell: (r) => <span className="num">{r.completed}</span> },
  { key: "day", header: "Nella giornata", align: "right", cell: (r) => <span className="num">{r.day_total}</span> },
];

function Cycles({ s }: { s: SystemHealthData["sections"]["cycles"] }) {
  const c = s.cycles;
  return (
    <Section id="cicli" section={s} link={{ to: "/risultati-agenti", label: "Risultati agenti" }}>
      {c && c.routines.length > 0 && <DataTable aria-label="Cicli per routine" columns={cycleColumns} rows={c.routines} rowKey={(r) => `${r.routine}-${r.agent}`} />}
      {c && (
        <p className="text-xs text-muted">
          Attesi: gli orari delle routine attive di Paperclip fino ad ora; partiti: le esecuzioni create oggi. Un ciclo che trova ancora aperto il precedente si unisce a
          quello e non crea un&apos;esecuzione nuova.
          {c.read_at && ` Routine lette alle ${when(c.read_at)}.`}
        </p>
      )}
    </Section>
  );
}

function Watchdog({ s }: { s: SystemHealthData["sections"]["watchdog"] }) {
  const w = s.warnings ?? [];
  return (
    <Section id="passaggi" section={s} link={{ to: "/notifiche", label: "Notifiche" }}>
      {w.length > 0 && (
        <ul className="space-y-1 text-sm" aria-label="Passaggi mancanti di oggi">
          {w.map((x, i) => (
            <li key={`${x.at ?? ""}-${i}`} className="break-words">
              {x.at && <span className="mr-2 text-xs text-muted">{when(x.at)}</span>}
              {x.text}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function Telegram({ s }: { s: SystemHealthData["sections"]["telegram"] }) {
  return (
    <Section id="telegram" section={s} link={{ to: "/notifiche", label: "Notifiche" }}>
      {s.configured && (
        <Facts
          items={[
            ["In coda", <span key="p" className="num">{s.pending ?? 0}</span>],
            ["Il più vecchio in coda", s.oldest_pending_age_s == null ? "—" : `da ${formatAge(s.oldest_pending_age_s)}`],
            ["Non inviati (24 ore)", <span key="f" className="num">{s.failed_24h ?? 0}</span>],
            ["Invio", s.paused ? "in pausa" : "attivo"],
          ]}
        />
      )}
    </Section>
  );
}

function Costs({ s }: { s: SystemHealthData["sections"]["costs"] }) {
  const m = s.month;
  return (
    <Section id="costi" section={s} link={{ to: "/costi-agenti", label: "Costi agenti" }}>
      <Facts
        items={[
          ["Spesa di oggi", s.today_usd != null ? formatUsd(s.today_usd) : "—"],
          ["Speso nel mese", m ? formatUsd(m.spent_usd) : "—"],
          ["Proiezione a fine mese", m ? formatUsd(m.projected_usd) : "—"],
          ["Budget mensile", m?.budget_usd ? formatUsd(m.budget_usd) : "non impostato"],
        ]}
      />
    </Section>
  );
}

function Mcp({ s }: { s: SystemHealthData["sections"]["mcp"] }) {
  return (
    <Section id="mcp" section={s} link={{ to: "/log-mcp", label: "Log MCP" }}>
      {s.by_code && s.by_code.length > 0 && (
        <ul className="flex flex-wrap gap-2 text-xs" aria-label="Errori per codice">
          {s.by_code.map((c) => (
            <li key={c.code} className="rounded-md border border-border px-2 py-0.5">
              <span className="font-mono">{c.code}</span> <span className="num">{c.count}</span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function Backup({ s }: { s: NonNullable<SystemHealthData["sections"]["backup"]> }) {
  const last = s.last_backup;
  const test = s.last_restore_test;
  return (
    <Section id="backup" section={s} link={{ to: "/impostazioni", label: "Impostazioni" }}>
      <Facts
        items={[
          ["Ultimo backup riuscito", s.last_ok_at ? `${when(s.last_ok_at)} (${s.last_ok_name ?? "—"})` : "—"],
          ["Ultima esecuzione", last ? `${when(last.started_at)}: ${last.status}${last.size_bytes != null ? `, ${formatBytes(last.size_bytes)}` : ""}${last.duration_s != null ? `, ${last.duration_s} s` : ""}` : "—"],
          ["Prova di ripristino", test ? `${when(test.started_at)}: ${test.status}` : "mai eseguita"],
        ]}
      />
      {s.destinations && s.destinations.length > 0 && (
        <ul className="space-y-1 text-sm" aria-label="Cartelle dei backup">
          {s.destinations.map((d) => (
            <li key={d.dir} className="break-words">
              <span className="font-mono text-xs">{d.dir}</span> <span className="text-muted">{d.error ? `— ${d.error}` : `— ${d.files} backup`}</span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

/** «Sentinella di sistema»: what it told the owner on Telegram and is still open, and its last pass. */
function Sentinel({ s }: { s: HealthSentinel }) {
  return (
    <Section id="sentinella" section={s} link={{ to: "/impostazioni", label: "Impostazioni" }}>
      <p className="text-xs text-muted">
        Ogni 5 minuti controlla salute del sistema, agenti Paperclip, cicli, errori MCP e nei log, identità e dati visibili agli agenti; avvisa su
        Telegram quando un problema inizia, lo ricorda ogni 2 ore e dice quando è risolto. Non conta nello stato complessivo.
      </p>
      <Facts
        items={[
          ["Ultimo giro", when(s.last_pass_at)],
          ["Avvisi negli ultimi 7 giorni", <span key="n" className="num">{s.alerts_7d}</span>],
          ...(s.last_error ? ([["Controlli non riusciti", s.last_error]] as [string, ReactNode][]) : []),
        ]}
      />
      {s.open.length > 0 && (
        <ul className="space-y-2 text-sm" aria-label="Problemi aperti della sentinella">
          {s.open.map((p) => (
            <li key={p.key} className="rounded-md border border-border px-3 py-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className={cn("font-medium", p.level === "critical" && "text-loss")}>{p.title}</span>
                <span className="text-xs text-muted">{`${p.label} · da ${when(p.since)}`}</span>
              </div>
              {p.detail && <p className="mt-1 whitespace-pre-line break-words text-xs text-muted">{p.detail}</p>}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function Version({ v }: { v: SystemHealthData["version"] }) {
  return (
    <Card role="region" aria-label="Versione">
      <CardHeader className="pb-2">
        <CardTitle className="text-base">Versione e ultimo avvio</CardTitle>
      </CardHeader>
      <CardContent>
        <Facts
          items={[
            ["Versione", <span key="b" className="font-mono">{v.build}</span>],
            ["Costruita", when(v.built_at)],
            ["Portale (api) avviato", when(v.api_started_at)],
            ["Worker avviato", when(v.worker_started_at)],
          ]}
        />
      </CardContent>
    </Card>
  );
}

/** «Salute del sistema»: services, data, agents, notifications, costs, at a glance. */
export default function SystemHealth() {
  const { data, isError, isFetching, refetch } = useSystemHealth();
  return (
    <div className="space-y-6">
      <PageHeader title="Salute del sistema">
        <div className="flex flex-wrap items-center gap-2">
          {data && <span className="text-xs text-muted">{`Controllato ${when(data.checked_at)}`}</span>}
          <Button size="sm" variant="outline" onClick={() => void refetch()} disabled={isFetching} aria-label="Aggiorna">
            <RefreshCw className={cn(isFetching && "animate-spin")} />
            <span className="max-sm:sr-only">Aggiorna</span>
          </Button>
        </div>
      </PageHeader>
      {isError && <p className="text-loss">Impossibile leggere la salute del sistema</p>}
      {!data ? (
        !isError && <p className="text-sm text-muted">Caricamento…</p>
      ) : (
        <>
          <Card role="region" aria-label="Stato complessivo">
            <CardContent className="flex flex-col gap-3 pt-6 sm:flex-row sm:items-start">
              <StatusBadge tone={HEALTH_TONE[data.status] ?? "neutral"} className="self-start px-3 py-1 text-sm">
                {HEALTH_LABEL[data.status] ?? data.status}
              </StatusBadge>
              {data.reasons.length === 0 ? (
                <p className="text-sm">Nessun problema: servizi, dati, agenti e notifiche funzionano.</p>
              ) : (
                <ul className="list-disc space-y-0.5 pl-5 text-sm" aria-label="Motivi">
                  {data.reasons.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
          <div className="grid gap-4 lg:grid-cols-2">
            <Services s={data.sections.services} />
            <div className="space-y-4">
              <Providers s={data.sections.providers} />
              <Quotes s={data.sections.quotes} />
              {data.sections.price_quality && <PriceQuality s={data.sections.price_quality} />}
              {data.sections.quotas && <Quotas s={data.sections.quotas} />}
            </div>
            <Paperclip s={data.sections.paperclip} />
            <Cycles s={data.sections.cycles} />
            <Watchdog s={data.sections.watchdog} />
            <Telegram s={data.sections.telegram} />
            <Costs s={data.sections.costs} />
            <Mcp s={data.sections.mcp} />
            {data.sections.backup && <Backup s={data.sections.backup} />}
            {data.sentinel && <Sentinel s={data.sentinel} />}
          </div>
          <Version v={data.version} />
        </>
      )}
    </div>
  );
}
