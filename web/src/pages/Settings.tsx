import { useState } from "react";
import { toast } from "sonner";
import { useIsAdmin } from "@/api/hooks/auth";
import { useAppSettings } from "@/api/hooks/risk";
import { useBackups, useDownloadBackup, useSaveMcpHideMode, useSaveTimezone, useVerifyIntegrity } from "@/api/hooks/system";
import type { BackupFile } from "@/api/types";
import { ChangePasswordSection } from "@/components/ChangePasswordSection";
import { PaperclipSection } from "@/components/PaperclipSection";
import { SessionsSection } from "@/components/SessionsSection";
import { TelegramExtrasSection } from "@/components/TelegramExtrasSection";
import { TotpSection } from "@/components/TotpSection";
import { UsersSection } from "@/components/UsersSection";
import { DataTable, type Column } from "@/components/DataTable";
import { FormField, selectClass } from "@/components/FormField";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { errorMessage } from "@/lib/errors";
import { formatBytes, formatDateTime } from "@/lib/format";
import { timezones } from "@/lib/timezones";

function TimezoneEditor({ initial }: { initial: string }) {
  const [tz, setTz] = useState(initial);
  const save = useSaveTimezone();
  const admin = useIsAdmin();
  if (!admin) return <p className="text-sm">{initial}</p>;
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

function McpHideModeSection({ initial }: { initial: boolean }) {
  const save = useSaveMcpHideMode();
  const admin = useIsAdmin();
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Agenti</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={initial}
            disabled={save.isPending || !admin}
            onChange={(e) => save.mutate(e.target.checked, { onSuccess: () => toast.success("Impostazione salvata") })}
            className="h-4 w-4 accent-[rgb(var(--accent))]"
          />
          Nascondi agli agenti la modalità dei portafogli (simulazione/paper)
        </label>
        <p className="text-muted">
          Se attivo, gli agenti Paperclip non vedono mai la modalità dei portafogli non reali (simulazione, paper): per loro è tutto reale. Nel portale e su Telegram la modalità resta sempre visibile.
        </p>
        {save.error && (
          <p role="alert" className="text-loss">
            {errorMessage(save.error)}
          </p>
        )}
      </CardContent>
    </Card>
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
  const download = useDownloadBackup();
  const admin = useIsAdmin();
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
        {admin && (
          <Button type="button" onClick={() => download.mutate()} disabled={download.isPending}>
            {download.isPending ? "Backup in corso…" : "Scarica backup ora"}
          </Button>
        )}
        {download.error && (
          <p role="alert" className="text-sm text-loss">
            {errorMessage(download.error)}
          </p>
        )}
        <h3 className="pt-2 text-sm font-medium">Backup automatici</h3>
        {isError && <p className="text-loss">Impossibile caricare l&apos;elenco dei backup</p>}
        <DataTable aria-label="Backup automatici" columns={columns} rows={data} rowKey={(b) => b.name} loading={isLoading} empty="Nessun backup automatico" />
      </CardContent>
    </Card>
  );
}

export default function Settings() {
  const { data, isError } = useAppSettings();
  const admin = useIsAdmin();
  return (
    <div>
      <PageHeader title="Impostazioni" />
      {admin && (
        <div className="mb-6">
          <UsersSection />
        </div>
      )}
      <Card role="region" aria-label="Fuso orario">
        <CardHeader>
          <CardTitle>Fuso orario</CardTitle>
        </CardHeader>
        <CardContent>
          {isError && <p className="text-loss">Impossibile caricare le impostazioni</p>}
          {data && <TimezoneEditor initial={data.timezone} />}
        </CardContent>
      </Card>
      {data && <McpHideModeSection initial={data.mcp_hide_mode} />}
      <PaperclipSection />
      {data?.weekly_goal && data.preopen_check && <TelegramExtrasSection goal={data.weekly_goal} preopen={data.preopen_check} watchdog={data.handoff_watchdog} report={data.weekly_report_pdf} />}
      <ChangePasswordSection />
      <TotpSection />
      <SessionsSection />
      <IntegritySection />
      <BackupSection />
    </div>
  );
}
