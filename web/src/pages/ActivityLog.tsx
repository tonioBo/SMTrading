import { Download, ScrollText } from "lucide-react";
import { useState, type ChangeEvent } from "react";
import { activityCsvUrl, useActivityCatalog, useActivityLog } from "@/api/hooks/activity";
import { useIsAdmin } from "@/api/hooks/auth";
import { useUsers } from "@/api/hooks/users";
import type { ActivityFilters, ActivityOutcome, ActivityRow } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { selectClass } from "@/components/FormField";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { errorMessage } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { ROLE_LABELS } from "@/lib/labels";

const NO_FILTERS: ActivityFilters = { email: "", category: "", outcome: "", from: "", to: "" };

const OUTCOME_LABELS: Record<ActivityOutcome, string> = { ok: "Riuscita", denied: "Negata", error: "Errore" };
const OUTCOME_VARIANTS = { ok: "secondary", denied: "warning", error: "destructive" } as const;

function target(row: ActivityRow): string {
  const entries = Object.entries(row.target ?? {});
  return entries.length ? entries.map(([k, v]) => `${k}: ${v}`).join(", ") : "—";
}

function details(row: ActivityRow): string {
  const parts = [row.error_message, Object.keys(row.summary ?? {}).length ? JSON.stringify(row.summary) : null];
  return parts.filter(Boolean).join(" · ") || "—";
}

/** «Registro attività» (admins only): every state-changing action of the portal users and their accesses. */
export default function ActivityLog() {
  const admin = useIsAdmin();
  const [filters, setFilters] = useState<ActivityFilters>(NO_FILTERS);
  const users = useUsers(admin);
  const catalog = useActivityCatalog(admin);
  const query = useActivityLog(filters, admin);
  const rows = query.data?.pages.flat();
  const set = (key: keyof ActivityFilters) => (e: ChangeEvent<HTMLSelectElement | HTMLInputElement>) =>
    setFilters((f) => ({ ...f, [key]: e.target.value }));

  if (!admin) {
    return (
      <div>
        <PageHeader title="Registro attività" />
        <p className="text-sm text-muted">Solo gli amministratori possono vedere il registro attività.</p>
      </div>
    );
  }

  const columns: Column<ActivityRow>[] = [
    { key: "ts", header: "Data", cell: (r) => <span className="num">{formatDateTime(r.ts)}</span> },
    { key: "user", header: "Utente", cell: (r) => r.email ?? "—" },
    { key: "role", header: "Ruolo", cell: (r) => (r.role ? (ROLE_LABELS[r.role] ?? r.role) : "—") },
    { key: "action", header: "Azione", cell: (r) => <span title={`${r.method} ${r.route}`}>{r.action_label}</span> },
    { key: "target", header: "Obiettivo", cell: (r) => <span className="font-mono text-xs">{target(r)}</span> },
    {
      key: "outcome",
      header: "Esito",
      cell: (r) => (
        <Badge variant={OUTCOME_VARIANTS[r.outcome]} title={r.error_code ?? undefined}>
          {OUTCOME_LABELS[r.outcome] ?? r.outcome}
          {r.status != null && r.outcome !== "ok" ? ` (${r.status})` : ""}
        </Badge>
      ),
    },
    { key: "ip", header: "IP", cell: (r) => <span className="font-mono text-xs">{r.ip ?? "—"}</span> },
    {
      key: "details",
      header: "Dettagli",
      cell: (r) => {
        const text = details(r);
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
      <PageHeader title="Registro attività" />
      <p className="mb-4 flex items-center gap-2 text-sm text-muted">
        <ScrollText className="h-4 w-4" aria-hidden />
        Ogni azione degli utenti del portale che cambia qualcosa, riuscita o no, e gli accessi. Mai password, codici o chiavi. Le voci non si
        cancellano.
      </p>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1">
              <Label htmlFor="activity-user">Utente</Label>
              <select id="activity-user" className={`${selectClass} w-56`} value={filters.email} onChange={set("email")}>
                <option value="">Tutti</option>
                {users.data?.map((u) => (
                  <option key={u.id} value={u.email}>
                    {u.email}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="activity-category">Tipo di azione</Label>
              <select id="activity-category" className={`${selectClass} w-48`} value={filters.category} onChange={set("category")}>
                <option value="">Tutti</option>
                {catalog.data?.categories.map((c) => (
                  <option key={c.key} value={c.key}>
                    {c.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="activity-outcome">Esito</Label>
              <select id="activity-outcome" className={`${selectClass} w-36`} value={filters.outcome} onChange={set("outcome")}>
                <option value="">Tutti</option>
                {Object.entries(OUTCOME_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="activity-from">Dal</Label>
              <Input id="activity-from" type="date" className="w-40" value={filters.from} max={filters.to || undefined} onChange={set("from")} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="activity-to">Al</Label>
              <Input id="activity-to" type="date" className="w-40" value={filters.to} min={filters.from || undefined} onChange={set("to")} />
            </div>
            <Button asChild variant="outline" className="ml-auto">
              <a href={activityCsvUrl(filters)} download>
                <Download />
                Esporta CSV
              </a>
            </Button>
          </div>
          {query.isError && <p className="text-sm text-loss">{errorMessage(query.error, "Impossibile caricare il registro")}</p>}
          <DataTable
            aria-label="Registro attività"
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
