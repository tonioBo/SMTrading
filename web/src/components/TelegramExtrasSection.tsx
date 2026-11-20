import { Link } from "react-router-dom";
import { useState } from "react";
import { toast } from "sonner";
import { useIsAdmin } from "@/api/hooks/auth";
import { useSaveTelegramExtras } from "@/api/hooks/system";
import type { HandoffWatchdog, PreopenCheck, WeeklyGoal, WeeklyReportPdf } from "@/api/types";
import { FormField, selectClass } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/errors";

const checkboxClass = "h-4 w-4 accent-[rgb(var(--accent))]";
const WEEKDAYS = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"];

/** «Messaggi Telegram»: the weekly goal of the summaries, the pre-open check, the
 * hand-off watchdog and the weekly PDF report. Admins edit, viewers only read. None
 * reaches the agents. */
export function TelegramExtrasSection({
  goal,
  preopen,
  watchdog,
  report,
}: {
  goal: WeeklyGoal;
  preopen: PreopenCheck;
  watchdog?: HandoffWatchdog;
  report?: WeeklyReportPdf;
}) {
  const admin = useIsAdmin();
  const save = useSaveTelegramExtras();
  const [pct, setPct] = useState(goal.pct);
  const [at, setAt] = useState(preopen.at);
  const saved = () => toast.success("Impostazione salvata");
  const cleanPct = pct.trim().replace(",", ".");
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Messaggi Telegram</CardTitle>
      </CardHeader>
      <CardContent className="space-y-5 text-sm">
        <section className="space-y-2" aria-label="Obiettivo settimanale">
          <h3 className="font-medium">Obiettivo settimanale</h3>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              className={checkboxClass}
              checked={goal.on}
              disabled={!admin || save.isPending}
              onChange={(e) => save.mutate({ weekly_goal: { on: e.target.checked } }, { onSuccess: saved })}
            />
            Mostra l&apos;obiettivo nel riepilogo delle 18:00 e in quello settimanale
          </label>
          {admin ? (
            <div className="flex flex-wrap items-end gap-2">
              <div className="w-40">
                <FormField label="Obiettivo (%)">
                  <Input inputMode="decimal" value={pct} onChange={(e) => setPct(e.target.value)} />
                </FormField>
              </div>
              <Button
                type="button"
                disabled={save.isPending || cleanPct === goal.pct || cleanPct === ""}
                onClick={() => save.mutate({ weekly_goal: { pct: cleanPct } }, { onSuccess: saved })}
              >
                Salva obiettivo
              </Button>
            </div>
          ) : (
            <p>Obiettivo: +{goal.pct.replace(".", ",")}% a settimana</p>
          )}
          <p className="text-muted">
            Calcolato sul valore in euro di tutti i portafogli attivi rispetto a quello di lunedì. È l&apos;obiettivo «settimanale»: gli altri
            (mese, trimestre, anno, date scelte, singoli portafogli) si gestiscono in{" "}
            <Link to="/obiettivi" className="text-accent hover:underline">
              Obiettivi
            </Link>
            .
          </p>
        </section>
        <section className="space-y-2" aria-label="Controllo pre-apertura">
          <h3 className="font-medium">Controllo pre-apertura</h3>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              className={checkboxClass}
              checked={preopen.on}
              disabled={!admin || save.isPending}
              onChange={(e) => save.mutate({ preopen_check: { on: e.target.checked } }, { onSuccess: saved })}
            />
            Invia il controllo pre-apertura dal lunedì al venerdì
          </label>
          {admin ? (
            <div className="flex flex-wrap items-end gap-2">
              <div className="w-40">
                <FormField label="Orario">
                  <Input type="time" value={at} onChange={(e) => setAt(e.target.value)} />
                </FormField>
              </div>
              <Button
                type="button"
                disabled={save.isPending || at === preopen.at || at === ""}
                onClick={() => save.mutate({ preopen_check: { at } }, { onSuccess: saved })}
              >
                Salva orario
              </Button>
            </div>
          ) : (
            <p>Orario: {preopen.at}</p>
          )}
          <p className="text-muted">Quotazioni, mercati del giorno, stato del sistema e blocchi, in un solo messaggio.</p>
        </section>
        {watchdog && (
          <section className="space-y-2" aria-label="Controllo dei passaggi">
            <h3 className="font-medium">Controllo dei passaggi</h3>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                className={checkboxClass}
                checked={watchdog.on}
                disabled={!admin || save.isPending}
                onChange={(e) => save.mutate({ handoff_watchdog: { on: e.target.checked } }, { onSuccess: saved })}
              />
              Avvisa su Telegram quando un passaggio tra agenti manca
            </label>
            <p className="text-muted">
              Legge i task di Paperclip ogni 5 minuti: AGENDA entro le 08:50 e BUONGIORNO entro le 09:20 nei giorni di borsa, PROTEGGI e RISCHIO
              OBIETTIVO senza risposta dopo 45 minuti, ESEGUI ancora aperti dopo 30 minuti a mercato aperto. Un avviso per passaggio al giorno;
              serve il lettore Paperclip attivo.
            </p>
          </section>
        )}
        {report && <WeeklyReportPart report={report} />}
        {save.error && (
          <p role="alert" className="text-loss">
            {errorMessage(save.error)}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

/** «Resoconto settimanale in PDF»: on/off, day and time. */
function WeeklyReportPart({ report }: { report: WeeklyReportPdf }) {
  const admin = useIsAdmin();
  const save = useSaveTelegramExtras();
  const [weekday, setWeekday] = useState(report.weekday);
  const [at, setAt] = useState(report.at);
  const saved = () => toast.success("Impostazione salvata");
  return (
    <section className="space-y-2" aria-label="Resoconto settimanale in PDF">
      <h3 className="font-medium">Resoconto settimanale in PDF</h3>
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          className={checkboxClass}
          checked={report.on}
          disabled={!admin || save.isPending}
          onChange={(e) => save.mutate({ weekly_report_pdf: { on: e.target.checked } }, { onSuccess: saved })}
        />
        Invia ogni settimana il resoconto in PDF nel gruppo
      </label>
      {admin ? (
        <div className="flex flex-wrap items-end gap-2">
          <div className="w-40">
            <FormField label="Giorno del resoconto">
              <select className={selectClass} value={weekday} onChange={(e) => setWeekday(Number(e.target.value))}>
                {WEEKDAYS.map((name, i) => (
                  <option key={name} value={i}>
                    {name}
                  </option>
                ))}
              </select>
            </FormField>
          </div>
          <div className="w-40">
            <FormField label="Orario del resoconto">
              <Input type="time" value={at} onChange={(e) => setAt(e.target.value)} />
            </FormField>
          </div>
          <Button
            type="button"
            disabled={save.isPending || at === "" || (at === report.at && weekday === report.weekday)}
            onClick={() => save.mutate({ weekly_report_pdf: { weekday, at } }, { onSuccess: saved })}
          >
            Salva giorno e orario
          </Button>
        </div>
      ) : (
        <p>
          Ogni {WEEKDAYS[report.weekday] ?? "venerdì"} alle {report.at}
        </p>
      )}
      <p className="text-muted">
        Il resoconto della settimana (da lunedì) come documento PDF, con una breve didascalia: P&amp;L totale, obiettivo settimanale e costo
        degli agenti. I portafogli fuori dai totali sono in una pagina a parte. Di default il venerdì alle 22:30, dopo la chiusura USA.
      </p>
      {save.error && (
        <p role="alert" className="text-loss">
          {errorMessage(save.error)}
        </p>
      )}
    </section>
  );
}
