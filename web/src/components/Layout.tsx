import { LogOut } from "lucide-react";
import { Outlet, useNavigate } from "react-router-dom";
import { useLogout, useMe } from "@/api/hooks/auth";
import { KillSwitchIndicator } from "@/components/KillSwitchIndicator";
import { ModeBanner } from "@/components/ModeBanner";
import { Sidebar } from "@/components/Sidebar";
import { ThemeToggle } from "@/components/ThemeToggle";
import { Button } from "@/components/ui/button";
import { useLiveSocket } from "@/lib/ws";
import { cn } from "@/lib/utils";

function LiveDot() {
  const { status } = useLiveSocket();
  const open = status === "open";
  const label = open ? "Aggiornamenti in tempo reale attivi" : "Aggiornamenti in tempo reale non connessi";
  return (
    <span
      role="status"
      aria-label={label}
      title={label}
      className={cn("inline-block h-2 w-2 rounded-full", open ? "bg-gain" : "bg-warn")}
    />
  );
}

/** Authenticated shell: the session is checked with /api/auth/me (401 → /login). */
export function Layout() {
  const me = useMe();
  const logout = useLogout();
  const navigate = useNavigate();

  if (!me.data) {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-muted">
        {me.isError && me.error && (me.error as { status?: number }).status !== 401
          ? "Impossibile verificare la sessione. Riprova più tardi."
          : "Caricamento…"}
      </div>
    );
  }

  const onLogout = () => logout.mutate(undefined, { onSettled: () => navigate("/login", { replace: true }) });

  return (
    <div className="flex h-screen">
      <aside className="flex w-60 shrink-0 flex-col border-r border-border">
        <div className="flex h-14 items-center gap-2 border-b border-border px-6 text-lg font-semibold">
          <span className="text-accent">SM</span>Trading
        </div>
        <Sidebar />
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-end gap-3 border-b border-border px-6">
          <LiveDot />
          <KillSwitchIndicator />
          <ThemeToggle />
          <span className="text-sm text-muted">{me.data.email}</span>
          <Button variant="ghost" size="sm" onClick={onLogout} disabled={logout.isPending}>
            <LogOut />
            Esci
          </Button>
        </header>
        <ModeBanner />
        <main className="flex-1 overflow-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
