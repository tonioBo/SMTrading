import { Link } from "react-router-dom";
import { useSignals } from "@/api/hooks/signals";
import type { SignalSymbol, StrategySignal } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDateTime, formatDecimal, formatQty } from "@/lib/format";
import { DIRECTION_LABELS, signalBadge, type SignalTone } from "@/lib/signals";
import { cn } from "@/lib/utils";

const TONE_CLASS: Record<SignalTone, string> = {
  now: "border-gain text-gain",
  closed: "border-gain text-gain",
  near: "border-warn text-warn",
  far: "border-border text-muted",
};

const STATUS_VARIANT = { ok: "secondary", "dati incompleti": "warning", "non supportata": "outline" } as const;

function value(v: string | null): string {
  return v == null || v === "" ? "—" : formatDecimal(v, 2);
}

function paramsText(params: Record<string, unknown>): string {
  const entries = Object.entries(params);
  return entries.length === 0 ? "—" : entries.map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(", ");
}

function SymbolRow({ s }: { s: SignalSymbol }) {
  const badge = signalBadge(s);
  return (
    <TableRow>
      <TableCell className="font-medium">{s.symbol}</TableCell>
      <TableCell className="num text-right">{formatQty(s.last)}</TableCell>
      <TableCell>{s.indicator}</TableCell>
      <TableCell className="num text-right">{value(s.value_closed)}</TableCell>
      <TableCell className="num text-right">{value(s.value_now)}</TableCell>
      <TableCell className="num">{`${DIRECTION_LABELS[s.direction] ?? s.direction} ${value(s.threshold)}`}</TableCell>
      <TableCell className="num text-right">{value(s.distance)}</TableCell>
      <TableCell>
        <Badge variant="outline" className={cn("whitespace-nowrap", TONE_CLASS[badge.tone])}>{`${badge.icon} ${badge.label}`}</Badge>
      </TableCell>
      <TableCell className="whitespace-nowrap text-muted">{s.as_of ? formatDateTime(s.as_of) : "—"}</TableCell>
    </TableRow>
  );
}

function StrategyBlock({ strategy }: { strategy: StrategySignal }) {
  return (
    <section aria-label={`Strategia ${strategy.ref}`} className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="num font-semibold">{strategy.ref}</span>
        <span>{strategy.name}</span>
        <span className="text-muted">{strategy.type}</span>
        <span className="num text-muted">{paramsText(strategy.params)}</span>
        <Badge variant="outline">{strategy.timeframe}</Badge>
        {strategy.status !== "ok" && <Badge variant={STATUS_VARIANT[strategy.status] ?? "outline"}>{strategy.status}</Badge>}
      </div>
      {strategy.symbols.length > 0 && (
        <Table aria-label={`Segnali ${strategy.ref}`}>
          <TableHeader>
            <TableRow>
              <TableHead>Simbolo</TableHead>
              <TableHead className="text-right">Ultimo</TableHead>
              <TableHead>Indicatore</TableHead>
              <TableHead className="text-right">Chiusura</TableHead>
              <TableHead className="text-right">Se chiudesse ora</TableHead>
              <TableHead>Soglia</TableHead>
              <TableHead className="text-right">Distanza</TableHead>
              <TableHead>Stato</TableHead>
              <TableHead>Aggiornato</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {strategy.symbols.map((s) => (
              <SymbolRow key={s.symbol} s={s} />
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  );
}

export default function Signals() {
  const { data, isError, isLoading } = useSignals();
  const portfolios = data?.portfolios ?? [];
  return (
    <div>
      <PageHeader title="Segnali">
        {data?.as_of && <span className="text-xs text-muted">{`ultimo aggiornamento ${formatDateTime(data.as_of)}`}</span>}
      </PageHeader>
      {isError && <p className="text-loss">Impossibile caricare i segnali</p>}
      {isLoading ? (
        <p className="text-sm text-muted">Caricamento…</p>
      ) : (
        data &&
        (portfolios.length === 0 ? (
          <p className="text-sm text-muted">Nessuna strategia approvata</p>
        ) : (
          <div className="space-y-6">
            {portfolios.map((p) => (
              <Card key={p.portfolio_id}>
                <CardHeader>
                  <CardTitle>
                    <Link to={`/portafogli/${p.portfolio_id}`} className="text-accent hover:underline">
                      {p.portfolio_name}
                    </Link>
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-6">
                  {p.strategies.map((s) => (
                    <StrategyBlock key={s.ref} strategy={s} />
                  ))}
                </CardContent>
              </Card>
            ))}
          </div>
        ))
      )}
    </div>
  );
}
