import { Download } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";
import {
  isReversible,
  transactionsCsvUrl,
  useIdempotencyKey,
  useReverseJournal,
  useTransactions,
  type TransactionFilters,
} from "@/api/hooks/portfolios";
import type { JournalType, Transaction } from "@/api/types";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { selectClass } from "@/components/FormField";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { errorMessage } from "@/lib/errors";
import { formatDateTime, formatMoney, formatQty } from "@/lib/format";

const JOURNAL_TYPE_LABELS: Record<JournalType, string> = {
  DEPOSIT: "Deposito",
  WITHDRAWAL: "Prelievo",
  TRADE: "Operazione",
  FEE: "Commissione",
  FX: "Cambio valuta",
  INTEREST: "Interessi",
  DIVIDEND: "Dividendo",
  CORPORATE_ACTION: "Evento societario",
  LIQUIDATION: "Liquidazione",
  RESET: "Reset",
  REVERSAL: "Storno",
};

const MONEY_KINDS = new Set(["cash", "collateral", "margin_loan"]);
const KIND_LABELS: Record<string, string> = {
  cash: "cash",
  position: "posizione",
  collateral: "collaterale",
  margin_loan: "prestito",
};

function Lines({ tx }: { tx: Transaction }) {
  return (
    <ul className="space-y-0.5">
      {tx.lines.map((l, i) => (
        <li key={i} className="num whitespace-nowrap">
          <span className="text-muted">{KIND_LABELS[l.kind] ?? l.kind}</span>{" "}
          {MONEY_KINDS.has(l.kind) ? formatMoney(l.quantity, l.asset) : `${formatQty(l.quantity)} ${l.asset}`}
        </li>
      ))}
    </ul>
  );
}

function ReverseDialog({ tx, portfolioId, onClose }: { tx: Transaction; portfolioId: string; onClose: () => void }) {
  const [reason, setReason] = useState("");
  const [missing, setMissing] = useState(false);
  const key = useIdempotencyKey();
  const reverse = useReverseJournal(portfolioId);
  const id = useId();
  const errorId = `${id}-error`;
  const fieldError = missing ? "La motivazione è obbligatoria" : reverse.error ? errorMessage(reverse.error) : null;

  return (
    <ConfirmDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="Stornare il movimento?"
      description={`${JOURNAL_TYPE_LABELS[tx.type] ?? tx.type} n. ${tx.seq} del ${formatDateTime(tx.created_at)}: viene registrato un movimento opposto.`}
      confirmLabel="Storna"
      destructive
      pending={reverse.isPending}
      onConfirm={() => {
        const trimmed = reason.trim();
        setMissing(!trimmed);
        if (!trimmed) return;
        reverse.mutate(
          { journalId: tx.id, reason: trimmed, key },
          {
            onSuccess: () => {
              toast.success("Movimento stornato");
              onClose();
            },
          },
        );
      }}
    >
      <div className="space-y-1.5">
        <Label htmlFor={id}>Motivazione</Label>
        <textarea
          id={id}
          value={reason}
          onChange={(e) => {
            setReason(e.target.value);
            if (e.target.value.trim()) setMissing(false);
            if (reverse.error) reverse.reset();
          }}
          rows={3}
          aria-invalid={fieldError ? true : undefined}
          aria-describedby={fieldError ? errorId : undefined}
          className="flex w-full rounded-md border border-border bg-transparent px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
        />
        {fieldError && (
          <p id={errorId} className="text-xs text-loss">
            {fieldError}
          </p>
        )}
      </div>
    </ConfirmDialog>
  );
}

const EMPTY_FILTERS: TransactionFilters = { type: "", from: "", to: "" };

/** `readOnly` (an archived portfolio): no reversals. */
export function Movements({ portfolioId, timezone, readOnly = false }: { portfolioId: string; timezone: string; readOnly?: boolean }) {
  const [filters, setFilters] = useState<TransactionFilters>(EMPTY_FILTERS);
  const query = useTransactions(portfolioId, filters, timezone);
  const [target, setTarget] = useState<Transaction | null>(null);
  const rows = query.data?.pages.flatMap((p) => p.items);
  const set = (field: keyof TransactionFilters) => (e: { target: { value: string } }) =>
    setFilters((f) => ({ ...f, [field]: e.target.value }));

  const columns: Column<Transaction>[] = [
    { key: "seq", header: "N.", cell: (t) => <span className="num">{t.seq}</span> },
    { key: "date", header: "Data", cell: (t) => <span className="num whitespace-nowrap">{formatDateTime(t.created_at)}</span> },
    {
      key: "type",
      header: "Tipo",
      cell: (t) => (
        <span className="flex flex-wrap items-center gap-1">
          {JOURNAL_TYPE_LABELS[t.type] ?? t.type}
          {t.reversed_by && <Badge variant="secondary">stornato</Badge>}
        </span>
      ),
    },
    { key: "lines", header: "Importi", cell: (t) => <Lines tx={t} /> },
    { key: "reason", header: "Motivazione", cell: (t) => t.reason ?? <span className="text-muted">—</span> },
    { key: "author", header: "Autore", cell: (t) => [t.author_type, t.author_id].filter(Boolean).join(": ") || "—" },
    {
      key: "actions",
      header: <span className="sr-only">Azioni</span>,
      align: "right",
      cell: (t) =>
        !readOnly && isReversible(t) ? (
          <Button size="sm" variant="outline" onClick={() => setTarget(t)}>
            Storna
          </Button>
        ) : null,
    },
  ];

  return (
    <Card>
      <CardContent className="space-y-4 pt-6">
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1.5">
            <Label htmlFor="tx-type">Tipo</Label>
            <select id="tx-type" className={`${selectClass} w-48`} value={filters.type} onChange={set("type")}>
              <option value="">Tutti</option>
              {Object.entries(JOURNAL_TYPE_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="tx-from">Dal</Label>
            <Input id="tx-from" type="date" value={filters.from} onChange={set("from")} className="w-40" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="tx-to">Al</Label>
            <Input id="tx-to" type="date" value={filters.to} onChange={set("to")} className="w-40" />
          </div>
          <Button asChild variant="outline" className="ml-auto">
            <a href={transactionsCsvUrl(portfolioId, filters, timezone)} download>
              <Download />
              Esporta CSV
            </a>
          </Button>
        </div>
        {query.isError && <p className="text-sm text-loss">{errorMessage(query.error, "Impossibile caricare i movimenti")}</p>}
        <DataTable
          aria-label="Movimenti"
          columns={columns}
          rows={rows}
          rowKey={(t) => t.id}
          loading={query.isLoading}
          empty="Nessun movimento"
        />
        {query.hasNextPage && (
          <div className="flex justify-center">
            <Button variant="outline" onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>
              {query.isFetchingNextPage ? "Caricamento…" : "Carica altri"}
            </Button>
          </div>
        )}
      </CardContent>
      {target && <ReverseDialog tx={target} portfolioId={portfolioId} onClose={() => setTarget(null)} />}
    </Card>
  );
}
