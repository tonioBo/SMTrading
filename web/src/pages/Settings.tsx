import { useState } from "react";
import { toast } from "sonner";
import { useAppSettings } from "@/api/hooks/risk";
import { useBackups, useSaveTimezone, useVerifyIntegrity } from "@/api/hooks/system";
import type { BackupFile } from "@/api/types";
import { DataTable, type Column } from "@/components/DataTable";
import { FormField, selectClass } from "@/components/FormField";
import { PageHeader } from "@/components/PageHeader";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { errorMessage } from "@/lib/errors";
import { formatBytes, formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const FALLBACK_TIMEZONES = ["Europe/Rome", "Europe/London", "Europe/Paris", "Europe/Berlin", "America/New_York", "America/Chicago", "America/Los_Angeles", "Asia/Tokyo", "UTC"];

function timezones(current: string): string[] {
  let list = FALLBACK_TIMEZONES;
  try {
    const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf?.("timeZone");
    if (supported?.length) list = supported;
  } catch {
    // keep the fallback list
  }
  return list.includes(current) ? list : [current, ...list];
}

function TimezoneEditor({ initial }: { initial: string }) {
  const [tz, setTz] = useState(initial);
  const save = useSaveTimezone();
  return (
    <div className="flex flex-wrap items-end gap-2">
      <div className="w-72">
        <FormField label="Fuso orario">
          <select className={selectClass} value={tz} onChange={(e) => setTz(e.target.value)}>
            {timezones(initial).map((z) => (
              <option key={z} value={z}>
                {z}
              </option>
            ))}
          </select>
        </FormField>
      </div>
      <Button type="button" disabled={save.isPending || tz === initial} onClick={() => save.mutate(tz, { onSuccess: () => toast.success("Fuso orario salvato") })}>
        Salva
      </Button>
      {save.error && (
        <p role="alert" className="w-full text-sm text-loss">
          {errorMessage(save.error)}
        </p>
      )}
    </div>
  );
}

function IntegritySection() {
  const verify = useVerifyIntegrity();
  const res = verify.data;
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Integrità dello storico</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <Button type="button" onClick={() => verify.mutate()} disabled={verify.isPending}>
          Verifica integrità
        </Button>
        {verify.error && (
          <p role="alert" className="text-loss">
            {errorMessage(verify.error)}
          </p>
        )}
        {res && (
          <ul className="space-y-1" aria-label="Esito della verifica">
            <li className={res.chain.ok ? "text-gain" : "text-loss"}>
              {res.chain.ok
                ? `Catena delle registrazioni integra (${res.chain.checked} verificate)`
                : `Catena delle registrazioni corrotta: primo diario errato ${res.chain.first_bad_journal_id ?? "—"}`}
            </li>
            <li className={res.balances.ok ? "text-gain" : "text-loss"}>
              {res.balances.ok ? "Saldi coerenti" : `Saldi non coerenti: ${res.balances.bad_accounts.join(", ")}`}
            </li>
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function BackupSection() {
  const { data, isLoading, isError } = useBackups();
  const columns: Column<BackupFile>[] = [
    { key: "name", header: "Nome", sortValue: (b) => b.name, cell: (b) => b.name },
    { key: "size", header: "Dimensione", align: "right", sortValue: (b) => b.size_bytes, cell: (b) => <span className="num">{formatBytes(b.size_bytes)}</span> },
    { key: "date", header: "Data", sortValue: (b) => b.modified_at, cell: (b) => <span className="num">{formatDateTime(b.modified_at)}</span> },
  ];
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Backup</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <a href="/api/system/backup" download className={cn(buttonVariants())}>
          Scarica backup ora
        </a>
        <h3 className="pt-2 text-sm font-medium">Backup automatici</h3>
        {isError && <p className="text-loss">Impossibile caricare l&apos;elenco dei backup</p>}
        <DataTable aria-label="Backup automatici" columns={columns} rows={data} rowKey={(b) => b.name} loading={isLoading} empty="Nessun backup automatico" />
      </CardContent>
    </Card>
  );
}

export default function Settings() {
  const { data, isError } = useAppSettings();
  return (
    <div>
      <PageHeader title="Impostazioni" />
      <Card>
        <CardHeader>
          <CardTitle>Fuso orario</CardTitle>
        </CardHeader>
        <CardContent>
          {isError && <p className="text-loss">Impossibile caricare le impostazioni</p>}
          {data && <TimezoneEditor initial={data.timezone} />}
        </CardContent>
      </Card>
      <IntegritySection />
      <BackupSection />
    </div>
  );
}
