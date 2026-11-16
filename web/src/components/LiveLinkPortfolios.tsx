import { useState } from "react";
import { toast } from "sonner";
import { ApiError } from "@/api/client";
import { useApplyPresetAfterLink, useBrokerFeePreset } from "@/api/hooks/fees";
import { useSetLivePortfolio } from "@/api/hooks/live";
import { usePortfolios } from "@/api/hooks/portfolios";
import type { LiveLink, LiveLinkPortfolio } from "@/api/types";
import { AdminOnly } from "@/components/AdminOnly";
import { FeePresetCheckbox } from "@/components/BrokerFeePreset";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { FormField, selectClass } from "@/components/FormField";
import { LiveTotpDialog } from "@/components/LiveTotpDialog";
import { PortfolioModeBadge } from "@/components/PortfolioModeBadge";
import { PortfolioStatusBadge } from "@/components/PortfolioStatusBadge";
import { Button } from "@/components/ui/button";
import { errorField, errorMessage } from "@/lib/errors";
import { formatMoney } from "@/lib/format";
import { useIsAdmin } from "@/api/hooks/auth";
import { cn } from "@/lib/utils";

const NO_POSITIONS_HINT = "Chiudi prima le posizioni reali";

function money(value: string | number | null | undefined, ccy: string | null): string {
  if (value == null || value === "") return "—";
  return ccy ? formatMoney(String(value), ccy) : String(value);
}

/** The amounts of a `field=cash` refusal (quotas with the new portfolio, broker cash, unassigned). */
function CashRefusal({ error, currency }: { error: unknown; currency: string | null }) {
  if (errorField(error) !== "cash" || !(error instanceof ApiError)) return null;
  const d = error.details as { quotas_total?: string; broker_cash?: string; unassigned?: string };
  if (d.quotas_total == null || d.broker_cash == null) return null;
  return (
    <p className="text-xs text-loss">
      Quote con questo portafoglio: {money(d.quotas_total, currency)} · contante al broker: {money(d.broker_cash, currency)} · non
      assegnato: {money(d.unassigned, currency)}
    </p>
  );
}

function LinkDialog({ link, onClose }: { link: LiveLink; onClose: () => void }) {
  const portfolios = usePortfolios();
  const setPf = useSetLivePortfolio();
  const [selected, setSelected] = useState("");
  const preset = useBrokerFeePreset(link.kind);
  const [usePreset, setUsePreset] = useState(true);
  const applyPreset = useApplyPresetAfterLink();
  const available = (portfolios.data ?? []).filter((p) => !p.broker_link_id && !p.archived_at && p.status !== "archived");
  const chosen = available.find((p) => p.id === selected);
  return (
    <LiveTotpDialog
      title={`Collegare un portafoglio a ${link.name}?`}
      description="Il portafoglio userà il conto reale del collegamento, con la sua quota di contante."
      confirmLabel="Collega"
      pending={setPf.isPending}
      confirmDisabled={!chosen}
      error={setPf.error}
      onClose={onClose}
      onConfirm={(totp) => {
        if (!chosen) return;
        setPf.mutate(
          { portfolioId: chosen.id, linkId: link.id, totp },
          {
            onSuccess: () => {
              toast.success(`${chosen.name} collegato a ${link.name}`);
              // a separate request after the link: never part of the link or arming itself
              if (preset && usePreset) applyPreset(chosen.id, preset);
              onClose();
            },
          },
        );
      }}
    >
      <FormField label="Portafoglio da collegare" hint="Leva 1, short non consentito, limiti per ordine e di perdita giornaliera">
        <select
          className={selectClass}
          value={selected}
          onChange={(e) => {
            setSelected(e.target.value);
            setPf.reset();
          }}
        >
          <option value="">Seleziona…</option>
          {available.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </FormField>
      <FeePresetCheckbox preset={preset} checked={usePreset} onChange={setUsePreset} />
      <CashRefusal error={setPf.error} currency={link.currency} />
    </LiveTotpDialog>
  );
}

function UnlinkButton({ link, pf }: { link: LiveLink; pf: LiveLinkPortfolio }) {
  const setPf = useSetLivePortfolio();
  const [open, setOpen] = useState(false);
  const busy = pf.positions_count > 0;
  return (
    <>
      <Button
        type="button"
        size="sm"
        variant="outline"
        aria-label={`Scollega ${pf.name}`}
        title={busy ? NO_POSITIONS_HINT : undefined}
        disabled={busy || setPf.isPending}
        onClick={() => {
          setPf.reset();
          setOpen(true);
        }}
      >
        Scollega
      </Button>
      {busy && <p className="mt-1 text-xs text-muted">{NO_POSITIONS_HINT}</p>}
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={`Scollegare ${pf.name} da ${link.name}?`}
        description="Il portafoglio non userà più il conto reale; la sua quota di contante torna non assegnata."
        confirmLabel="Scollega"
        pending={setPf.isPending}
        error={setPf.error ? errorMessage(setPf.error) : null}
        onConfirm={() =>
          setPf.mutate(
            { portfolioId: pf.id, linkId: null },
            {
              onSuccess: () => {
                toast.success(`${pf.name} scollegato`);
                setOpen(false);
              },
            },
          )
        }
      />
    </>
  );
}

/**
 * «Portafogli collegati» of a real-money link: each linked portfolio with its cash quota in
 * the account currency, the broker's cash, what is assigned and what is left. Linking (TOTP)
 * is possible in dry run or armed; unlinking needs no open real positions. Admins only act.
 */
export function LiveLinkPortfolios({ link }: { link: LiveLink }) {
  const portfolios = usePortfolios();
  const admin = useIsAdmin();
  const [linking, setLinking] = useState(false);
  const canLink = link.live_state === "dry_run" || link.live_state === "armed";
  const byId = new Map((portfolios.data ?? []).map((p) => [p.id, p]));
  const free = link.unassigned;
  const negative = free != null && String(free).trim().startsWith("-");
  return (
    <section aria-label="Portafogli collegati" className="space-y-2">
      <h4 className="text-sm font-medium">Portafogli collegati</h4>
      {link.portfolios.length === 0 ? (
        <p className="text-sm text-muted">Nessun portafoglio collegato</p>
      ) : (
        <table className="w-full text-xs">
          <thead className="text-left text-muted">
            <tr>
              <th className="py-1 pr-2 font-normal">Portafoglio</th>
              <th className="py-1 pr-2 font-normal">Stato</th>
              <th className="py-1 pr-2 text-right font-normal">Quota</th>
              <th className="py-1 font-normal">{admin ? <span className="sr-only">Azioni</span> : null}</th>
            </tr>
          </thead>
          <tbody>
            {link.portfolios.map((p) => {
              const full = byId.get(p.id);
              return (
                <tr key={p.id} className="border-t border-border align-top">
                  <td className="py-1 pr-2">
                    <div className="font-medium">{p.name}</div>
                    {full && <PortfolioModeBadge mode={full.mode} liveState={full.live_state} label={full.mode_label} className="mt-0.5" />}
                  </td>
                  <td className="py-1 pr-2">
                    <PortfolioStatusBadge status={p.status} />
                  </td>
                  <td className="num py-1 pr-2 text-right">{money(p.quota, link.currency)}</td>
                  <td className="py-1 text-right">
                    <AdminOnly>
                      <UnlinkButton link={link} pf={p} />
                    </AdminOnly>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <div className="space-y-0.5 text-xs">
        <div>Contante al broker: {money(link.broker_cash, link.currency)}</div>
        <div>Assegnato ai portafogli: {money(link.quotas_total, link.currency)}</div>
        <div className={cn(negative && "font-semibold text-loss")}>
          Non assegnato: {money(free, link.currency)}
          {negative && " — le quote superano il contante del conto"}
        </div>
      </div>
      <AdminOnly>
        <Button
          type="button"
          size="sm"
          disabled={!canLink}
          title={canLink ? undefined : "Avvia la prova a secco o attiva il denaro reale per collegare"}
          onClick={() => setLinking(true)}
        >
          Collega un portafoglio
        </Button>
        {!canLink && <p className="text-xs text-muted">Avvia la prova a secco o attiva il denaro reale per collegare</p>}
      </AdminOnly>
      {linking && <LinkDialog link={link} onClose={() => setLinking(false)} />}
    </section>
  );
}
