import { LogOut, Menu, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useEffect, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { useLogout, useMe } from "@/api/hooks/auth";
import { KillSwitchIndicator } from "@/components/KillSwitchIndicator";
import { ModeBanner } from "@/components/ModeBanner";
import { NewVersionBanner } from "@/components/NewVersionBanner";
import { Sidebar } from "@/components/Sidebar";
import { ThemeToggle } from "@/components/ThemeToggle";
import { Button } from "@/components/ui/button";
import { useSidebarCollapsed } from "@/lib/sidebarState";
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
function Brand({ short = false }: { short?: boolean }) {
  return (
    <span className="text-lg font-semibold">
      <span className="text-accent">SM</span>
      {short ? null : "Trading"}
    </span>
  );
}

/** Narrow screens: the sidebar as an overlay drawer (closes on navigation, Esc, backdrop). */
function MobileDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 md:hidden" role="dialog" aria-modal="true" aria-label="Menu">
      <button type="button" aria-label="Chiudi menu" className="absolute inset-0 bg-black/50" onClick={onClose} />
      <aside className="absolute inset-y-0 left-0 flex w-64 max-w-[85vw] flex-col border-r border-border bg-bg shadow-lg">
        <div className="flex h-14 items-center border-b border-border px-6">
          <Brand />
        </div>
        <Sidebar label="Navigazione principale (menu)" />
      </aside>
    </div>
  );
}

export function Layout() {
  const me = useMe();
  const logout = useLogout();
  const navigate = useNavigate();
  const location = useLocation();
  const [collapsed, toggleCollapsed] = useSidebarCollapsed();
  const [drawerOpen, setDrawerOpen] = useState(false);
  useEffect(() => setDrawerOpen(false), [location.pathname]); // closes on navigation

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
      <aside
        className={cn(
          "hidden shrink-0 flex-col border-r border-border transition-[width] md:flex",
          collapsed ? "w-16" : "w-60",
        )}
      >
        <div className={cn("flex h-14 items-center border-b border-border", collapsed ? "justify-center px-2" : "justify-between px-4")}>
          {!collapsed && <Brand />}
          <Button
            variant="ghost"
            size="sm"
            aria-label={collapsed ? "Espandi menu" : "Comprimi menu"}
            title={collapsed ? "Espandi menu" : "Comprimi menu"}
            aria-expanded={!collapsed}
            onClick={toggleCollapsed}
          >
            {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
          </Button>
        </div>
        <Sidebar collapsed={collapsed} />
      </aside>
      <MobileDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-end gap-3 border-b border-border px-4 md:px-6">
          <Button
            variant="ghost"
            size="sm"
            className="mr-auto md:hidden"
            aria-label="Apri menu"
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen(true)}
          >
            <Menu />
          </Button>
          <LiveDot />
          <KillSwitchIndicator />
          <ThemeToggle />
          <span className="hidden text-sm text-muted sm:inline">{me.data.email}</span>
          <Button variant="ghost" size="sm" onClick={onLogout} disabled={logout.isPending}>
            <LogOut />
            Esci
          </Button>
        </header>
        <NewVersionBanner />
        <ModeBanner />
        <main className="flex-1 overflow-auto p-4 md:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
