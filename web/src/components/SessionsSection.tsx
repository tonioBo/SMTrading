import { toast } from "sonner";
import { useRevokeOtherSessions } from "@/api/hooks/auth";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { errorMessage } from "@/lib/errors";

export function SessionsSection() {
  const revoke = useRevokeOtherSessions();
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>Sessioni</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p className="text-muted">Una sessione dura al massimo 24 ore (12 ore senza attività).</p>
        <Button
          type="button"
          variant="outline"
          disabled={revoke.isPending}
          onClick={() =>
            revoke.mutate(undefined, {
              onSuccess: ({ revoked }) => toast.success(revoked === 0 ? "Nessun'altra sessione aperta" : revoked === 1 ? "Chiusa 1 altra sessione" : `Chiuse ${revoked} altre sessioni`),
            })
          }
        >
          Chiudi tutte le altre sessioni
        </Button>
        {revoke.error && (
          <p role="alert" className="text-loss">
            {errorMessage(revoke.error)}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
