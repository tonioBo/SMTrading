import { ChevronDown } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import {
  useRoadmap,
  type RoadmapItem,
  type RoadmapLive,
  type RoadmapOwner,
  type RoadmapStage,
  type RoadmapStatus,
} from "@/api/hooks/roadmap";
import { PageHeader } from "@/components/PageHeader";
import { Card, CardContent } from "@/components/ui/card";
import { formatMoney, formatPct } from "@/lib/format";
import { cn } from "@/lib/utils";

const STATUS_LABEL: Record<RoadmapStatus, string> = {
  fatto: "Fatto",
  in_corso: "In corso",
  prossimo: "Prossimo",
  futuro: "Futuro",
};

const STATUS_TONE: Record<RoadmapStatus, string> = {
  fatto: "border-transparent bg-gain/15 text-gain",
  in_corso: "border-transparent bg-warn/15 text-warn",
  prossimo: "border-border text-fg",
  futuro: "border-dashed border-border text-muted",
};

const DOT_TONE: Record<RoadmapStatus, string> = {
  fatto: "border-gain bg-gain",
  in_corso: "border-warn bg-warn",
  prossimo: "border-fg bg-bg",
  futuro: "border-dashed border-muted bg-bg",
};

const OWNER_LABEL: Record<RoadmapOwner, string> = {
  smtrading: "SMTrading",
  neurograph: "Luca · Neurograph",
  entrambi: "SMTrading e Neurograph",
  matteo: "Matteo",
};

const KIND_LABEL: Record<string, string> = {
  esercitazione: "Esercitazione",
  esperimento: "Esperimento",
  strumento: "Strumento",
  criterio: "Criterio",
  prova: "Prova",
};

/** "2026-11-28" → "28/11" (with the year: "28/11/2026"). */
function shortDate(iso: string, year = false): string {
  const [y, m, d] = iso.split("-");
  return year ? `${d}/${m}/${y}` : `${d}/${m}`;
}

function stageDates(s: RoadmapStage): string {
  if (!s.start) return s.end ? `fino al ${shortDate(s.end)}` : "";
  if (!s.end) return `dal ${shortDate(s.start)}`;
  if (s.end === s.start) return shortDate(s.start);
  return `${shortDate(s.start)} → ${shortDate(s.end)}`;
}

function StatusBadge({ status }: { status: RoadmapStatus }) {
  return (
    <span className={cn("inline-flex shrink-0 items-center rounded-md border px-2 py-0.5 text-xs font-semibold", STATUS_TONE[status])}>
      {STATUS_LABEL[status]}
    </span>
  );
}

const DASH = "—";

function Tile({ label, value, sub }: { label: string; value: string; sub?: ReactNode }) {
  return (
    <div className="min-w-0 rounded-lg border border-border bg-card p-3">
      <div className="text-xs font-medium uppercase tracking-wide text-muted">{label}</div>
      <div className="num mt-1 break-words text-lg font-semibold">{value}</div>
      {sub && <div className="mt-0.5 break-words text-xs text-muted">{sub}</div>}
    </div>
  );
}

function LiveNumbers({ live, goalLabel }: { live: RoadmapLive; goalLabel: string }) {
  const c = live.campaign;
  const campaign =
    c.state === "prima" ? "Non iniziata" : c.state === "conclusa" ? "Conclusa" : `Giorno ${c.day} di ${c.total_days}`;
  const g = live.goal;
  const equity = live.equity ? formatMoney(live.equity.current_eur, "EUR") : DASH;
  const goalSub =
    g && g.target_eur && g.pct ? (
      <>
        traguardo {formatMoney(g.target_eur, "EUR")} · {formatPct(g.pct)} su {formatPct(g.target_pct)} · {g.status}
      </>
    ) : (
      `obiettivo «${goalLabel}»: ${DASH}`
    );
  const d = live.drills;
  const days = live.go_no_go.days_left;
  return (
    <section aria-label="Numeri dal vivo" className="mb-6">
      <div className="grid grid-cols-1 gap-3 min-[420px]:grid-cols-2 lg:grid-cols-5">
        <Tile label="Campagna" value={campaign} sub={`${shortDate(c.start)} → ${shortDate(c.end)}`} />
        <Tile label="Valore dei portafogli nel totale" value={equity} sub={goalSub} />
        <Tile
          label="Esercitazioni fatte"
          value={`${d.done} su ${d.planned}`}
          sub={d.next ? `prossima ${shortDate(d.next.date)}: ${d.next.title}` : "nessuna in programma"}
        />
        <Tile
          label="Decisione «sì/no»"
          value={days === 0 ? "oggi o passata" : days === 1 ? "domani" : `tra ${days} giorni`}
          sub={shortDate(live.go_no_go.date, true)}
        />
        <Tile
          label="Neurograph"
          value={`${live.neurograph.count} in uso`}
          sub={
            live.neurograph.tools.length ? (
              <span className="font-mono">{live.neurograph.tools.join(", ")}</span>
            ) : (
              "nessuno strumento"
            )
          }
        />
      </div>
      {live.notes.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs text-muted">
          {live.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Item({ item }: { item: RoadmapItem }) {
  return (
    <li className="min-w-0 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="min-w-0 break-words font-medium">{item.title}</span>
        <StatusBadge status={item.status} />
        {item.kind && KIND_LABEL[item.kind] && <span className="text-xs text-muted">{KIND_LABEL[item.kind]}</span>}
        {item.date && <span className="num text-xs text-muted">{shortDate(item.date)}</span>}
        {item.owner && <span className="text-xs text-muted">{OWNER_LABEL[item.owner]}</span>}
      </div>
      <p className="mt-1 break-words text-sm leading-relaxed">{item.detail}</p>
      {item.refs.length > 0 && (
        <ul aria-label={`Documenti di ${item.title}`} className="mt-2 space-y-0.5">
          {item.refs.map((r) => (
            <li key={r} className="break-all font-mono text-xs text-muted">
              {r}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function Stage({ stage, here, last }: { stage: RoadmapStage; here: boolean; last: boolean }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const done = stage.items.filter((i) => i.status === "fatto").length;
  return (
    <li aria-current={here ? "step" : undefined} className="relative min-w-0 pb-6 pl-8">
      {/* the line of the road and the stage's dot */}
      {!last && <span aria-hidden className="absolute bottom-0 left-[7px] top-5 w-0.5 bg-border" />}
      <span
        aria-hidden
        className={cn("absolute left-0 top-1 h-4 w-4 rounded-full border-2", DOT_TONE[stage.status], here && "ring-4 ring-warn/30")}
      />
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <h2 className="min-w-0 break-words text-base font-semibold sm:text-lg">{stage.title}</h2>
        <StatusBadge status={stage.status} />
        {here && <span className="rounded-md bg-warn px-2 py-0.5 text-xs font-semibold text-black">Sei qui</span>}
      </div>
      <div className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-muted">
        {stage.start && <span className="num">{stageDates(stage)}</span>}
        <span>{OWNER_LABEL[stage.owner]}</span>
      </div>
      <p className="mt-2 max-w-prose break-words text-sm leading-relaxed">{stage.what}</p>
      <p className="mt-1 max-w-prose break-words text-sm leading-relaxed text-muted">
        <span className="font-medium text-fg">Perché conta: </span>
        {stage.why}
      </p>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        aria-label={`Dettagli: ${stage.title}`}
        onClick={() => setOpen((v) => !v)}
        className="mt-2 inline-flex min-h-[36px] items-center gap-1 rounded-md px-2 text-sm text-accent hover:bg-card"
      >
        <ChevronDown className={cn("h-4 w-4 transition-transform", !open && "-rotate-90")} aria-hidden />
        Dettagli ({done} su {stage.items.length} fatti)
      </button>
      {open && (
        <ul id={id} aria-label={`Voci di ${stage.title}`} className="mt-2 space-y-2">
          {stage.items.map((item, i) => (
            <Item key={`${i}-${item.title}`} item={item} />
          ))}
        </ul>
      )}
    </li>
  );
}

export default function Roadmap() {
  const { data, isLoading, isError } = useRoadmap();
  if (isError) {
    return (
      <div>
        <PageHeader title="Roadmap" />
        <p className="text-loss">Impossibile caricare la roadmap</p>
      </div>
    );
  }
  if (isLoading || !data) {
    return (
      <div>
        <PageHeader title="Roadmap" />
        <p className="text-muted">Caricamento…</p>
      </div>
    );
  }
  const hereId = data.stages.find((s) => s.status === "in_corso")?.id;
  return (
    <div className="min-w-0">
      <PageHeader title="Roadmap" />
      <Card className="mb-4">
        <CardContent className="pt-6">
          <div className="text-xs font-medium uppercase tracking-wide text-muted">Dove siamo</div>
          <p className="mt-1 max-w-prose break-words text-base leading-relaxed">{data.summary}</p>
          <p className="mt-2 text-xs text-muted">Aggiornata il {shortDate(data.updated, true)}</p>
        </CardContent>
      </Card>
      <LiveNumbers live={data.live} goalLabel={data.campaign.goal.label} />
      <section aria-label="La strada" className="mb-8">
        <ol className="min-w-0">
          {data.stages.map((s, i) => (
            <Stage key={s.id} stage={s} here={s.id === hereId} last={i === data.stages.length - 1} />
          ))}
        </ol>
      </section>
      <section aria-label="Glossario">
        <h2 className="mb-3 text-base font-semibold sm:text-lg">Glossario</h2>
        <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
          {data.glossary.map((g) => (
            <div key={g.term} className="min-w-0">
              <dt className="break-words text-sm font-semibold">{g.term}</dt>
              <dd className="break-words text-sm text-muted">{g.meaning}</dd>
            </div>
          ))}
        </dl>
      </section>
    </div>
  );
}
