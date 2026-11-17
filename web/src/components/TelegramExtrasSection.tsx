import { Link } from "react-router-dom";
import { useState } from "react";
import { toast } from "sonner";
import { useIsAdmin } from "@/api/hooks/auth";
import { useSaveTelegramExtras } from "@/api/hooks/system";
import type { PreopenCheck, WeeklyGoal } from "@/api/types";
import { FormField } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/errors";

const checkboxClass = "h-4 w-4 accent-[rgb(var(--accent))]";

/** «Messaggi Telegram»: the weekly goal of the summaries and the pre-open check. Admins
 * edit, viewers only read. Neither reaches the agents. */
export function TelegramExtrasSection({ goal, preopen }: { goal: WeeklyGoal; preopen: PreopenCheck }) {
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
        {save.error && (
          <p role="alert" className="text-loss">
            {errorMessage(save.error)}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
