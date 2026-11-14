import { Download, ShieldAlert } from "lucide-react";
import { useState, type ChangeEvent } from "react";
import { liveAuditCsvUrl, useLiveAudit, useLiveLinks } from "@/api/hooks/live";
import { usePortfolios } from "@/api/hooks/portfolios";
import type { LiveAuditFilters, LiveAuditRow } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { selectClass } from "@/components/FormField";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { LIVE_AUDIT_ACTIONS } from "@/lib/live";

const NO_FILTERS: LiveAuditFilters = { link_id: "", portfolio_id: "", action: "" };

function details(value: unknown): string {
  if (value == null) return "—";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text === "{}" ? "—" : text;
}

/** «Registro denaro reale»: every step of the real-money links, filterable, CSV export. */
export default function LiveAudit() {
  const [filters, setFilters] = useState<LiveAuditFilters>(NO_FILTERS);
  const links = useLiveLinks();
  const portfolios = usePortfolios({ includeArchived: true });
  const query = useLiveAudit(filters);
  const rows = query.data?.pages.flat();
  const set = (key: keyof LiveAuditFilters) => (e: ChangeEvent<HTMLSelectElement>) => setFilters((f) => ({ ...f, [key]: e.target.value }));

  const columns: Column<LiveAuditRow>[] = [
    { key: "ts", header: "Data", cell: (r) => <span className="num">{formatDateTime(r.ts)}</span> },
    { key: "link", header: "Collegamento", cell: (r) => r.link_name },
    { key: "portfolio", header: "Portafoglio", cell: (r) => r.portfolio_name ?? "—" },
    { key: "action", header: "Azione", cell: (r) => LIVE_AUDIT_ACTIONS[r.action] ?? r.action },
    { key: "actor", header: "Attore", cell: (r) => r.actor },
    { key: "ip", header: "IP", cell: (r) => <span className="font-mono text-xs">{r.ip ?? "—"}</span> },
    {
      key: "details",
      header: "Dettagli",
      cell: (r) => {
        const text = details(r.details);
        return (
          <span title={text} className="block max-w-md truncate font-mono text-xs">
            {text}
          </span>
        );
      },
    },
  ];

  return (
    <div>
      <PageHeader title="Registro denaro reale" />
      <p className="mb-4 flex items-center gap-2 text-sm text-muted">
        <ShieldAlert className="h-4 w-4 text-loss" aria-hidden />
        Ogni passo dei collegamenti con denaro reale: chiavi, test, attivazioni, collegamenti, sospensioni e ordini inviati. Mai le chiavi.
      </p>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label htmlFor="audit-link">Collegamento</Label>
              <select id="audit-link" className={`${selectClass} w-48`} value={filters.link_id} onChange={set("link_id")}>
                <option value="">Tutti</option>
                {links.data?.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="audit-portfolio">Portafoglio</Label>
              <select id="audit-portfolio" className={`${selectClass} w-48`} value={filters.portfolio_id} onChange={set("portfolio_id")}>
                <option value="">Tutti</option>
                {portfolios.data?.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="audit-action">Azione</Label>
              <select id="audit-action" className={`${selectClass} w-56`} value={filters.action} onChange={set("action")}>
                <option value="">Tutte</option>
                {Object.entries(LIVE_AUDIT_ACTIONS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <Button asChild variant="outline" className="ml-auto">
              <a href={liveAuditCsvUrl(filters)} download>
                <Download />
                Esporta CSV
              </a>
            </Button>
          </div>
          {query.isError && <p className="text-sm text-loss">{errorMessage(query.error, "Impossibile caricare il registro")}</p>}
          <DataTable
            aria-label="Registro denaro reale"
            columns={columns}
            rows={rows}
            rowKey={(r) => String(r.id)}
            loading={query.isLoading}
            empty="Nessuna voce nel registro"
          />
          {query.hasNextPage && (
            <div className="flex justify-center">
              <Button variant="outline" onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>
                {query.isFetchingNextPage ? "Caricamento…" : "Carica altri"}
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
