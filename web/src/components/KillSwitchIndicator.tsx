import { ShieldAlert, ShieldCheck } from "lucide-react";
import { useKillSwitch } from "@/api/hooks/killSwitch";
import { Badge } from "@/components/ui/badge";

export function KillSwitchIndicator() {
  const { data } = useKillSwitch();
  if (!data) return null;
  return data.on ? (
    <Badge variant="destructive" className="gap-1" title="Tutti gli ordini degli agenti sono bloccati">
      <ShieldAlert className="h-3.5 w-3.5" aria-hidden />
      Kill-switch attivo
    </Badge>
  ) : (
    <Badge variant="outline" className="gap-1 text-gain">
      <ShieldCheck className="h-3.5 w-3.5" aria-hidden />
      Trading attivo
    </Badge>
  );
}
