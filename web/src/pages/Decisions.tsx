import { PageHeader } from "@/components/PageHeader";
import { DecisionsSection } from "@/pages/Notifications";

/** «Decisioni»: every decision the agents log (proposals, approvals, vetoes, strategies,
 * lessons and team rules), with the same filters as in «Notifiche». */
export default function Decisions() {
  return (
    <div className="space-y-6">
      <PageHeader title="Decisioni" />
      <p className="text-sm text-muted">
        Le decisioni registrate dagli agenti. Filtra per tipo «Lezione» per gli errori che hanno imparato, «Regola del team» per le regole che ne sono nate.
      </p>
      <DecisionsSection />
    </div>
  );
}
