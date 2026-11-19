import { FileDown } from "lucide-react";
import { useState } from "react";
import { usePortfolios } from "@/api/hooks/portfolios";
import { FormField } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { addDays, periodReportUrl, romeToday } from "@/lib/campaigns";

/** «Resoconto»: the PDF report of any date range and portfolios (`GET /api/reports/period.pdf`). */
function ReportForm({ onClose }: { onClose: () => void }) {
  const today = romeToday();
  const [from, setFrom] = useState(addDays(today, -29));
  const [to, setTo] = useState(today);
  const portfolios = usePortfolios();
  // none ticked = every live portfolio
  const [picked, setPicked] = useState<string[]>([]);
  const error = !from || !to ? "Indica le due date" : to < from ? "La data di fine precede quella di inizio" : null;
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <FormField label="Dal">
          <Input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        </FormField>
        <FormField label="Al">
          <Input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
        </FormField>
      </div>
      <fieldset className="space-y-1.5">
        <legend className="text-sm font-medium">Portafogli</legend>
        <p className="text-xs text-muted">Nessuno selezionato: tutti i portafogli attivi.</p>
        <div className="grid max-h-48 gap-1 overflow-y-auto sm:grid-cols-2">
          {portfolios.data?.map((p) => (
            <label key={p.id} className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={picked.includes(p.id)} onChange={() => toggle(p.id)} />
              {p.name}
            </label>
          ))}
        </div>
      </fieldset>
      {error && (
        <p role="alert" className="text-sm text-loss">
          {error}
        </p>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>
          Chiudi
        </Button>
        {error ? (
          <Button type="button" disabled>
            <FileDown />
            Scarica PDF
          </Button>
        ) : (
          <Button asChild>
            <a href={periodReportUrl(from, to, picked)} download>
              <FileDown />
              Scarica PDF
            </a>
          </Button>
        )}
      </DialogFooter>
    </div>
  );
}

/** The «Resoconto» button (Dashboard, Portafogli, Campagne): opens the period dialog. */
export function ReportButton({ variant = "outline" }: { variant?: "outline" | "default" }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button type="button" variant={variant} onClick={() => setOpen(true)}>
        <FileDown />
        Resoconto
      </Button>
      {open && (
        <Dialog open onOpenChange={(o) => !o && setOpen(false)}>
          <DialogContent className="max-h-[90dvh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>Resoconto PDF</DialogTitle>
              <DialogDescription>
                Rendimento, confronto con SPY, QQQ e VWCE, operazioni, strategie, obiettivi, costi degli agenti, lezioni
                e decisioni del periodo.
              </DialogDescription>
            </DialogHeader>
            <ReportForm onClose={() => setOpen(false)} />
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}
