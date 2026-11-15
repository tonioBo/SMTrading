import { LogOut, Menu, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { useLogout, useMe } from "@/api/hooks/auth";
import { ChangePasswordSection } from "@/components/ChangePasswordSection";
import { KillSwitchIndicator } from "@/components/KillSwitchIndicator";
import { ModeBanner } from "@/components/ModeBanner";
import { NewVersionBanner } from "@/components/NewVersionBanner";
import { PaperclipBanner } from "@/components/PaperclipBanner";
import { Logo } from "@/components/Logo";
import { Sidebar } from "@/components/Sidebar";
import { ThemeToggle } from "@/components/ThemeToggle";
import { Badge } from "@/components/ui/badge";
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
    <span className="flex items-center gap-2 text-lg font-semibold">
      <Logo />
      <span>
        <span className="text-accent">SM</span>
        {short ? null : "Trading"}
      </span>
    </span>
  );
}

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

/**
 * Narrow screens: the sidebar as an overlay drawer (closes on navigation, Esc, backdrop).
 * Focus moves into it on open, Tab stays inside, and returns to `returnFocus` on close.
 */
function MobileDrawer({ open, onClose, returnFocus }: { open: boolean; onClose: () => void; returnFocus: RefObject<HTMLElement> }) {
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!open) return;
    const items = () => Array.from(panel.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    items()[0]?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const list = items();
      if (list.length === 0) return;
      const first = list[0];
      const last = list[list.length - 1];
      const inside = panel.current?.contains(document.activeElement);
      if (e.shiftKey && (document.activeElement === first || !inside)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !inside)) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    const target = returnFocus.current;
    return () => {
      window.removeEventListener("keydown", onKey);
      target?.focus();
    };
  }, [open, onClose, returnFocus]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 md:hidden" role="dialog" aria-modal="true" aria-label="Menu">
      <button type="button" tabIndex={-1} aria-label="Chiudi menu" className="absolute inset-0 bg-black/50" onClick={onClose} />
      <aside ref={panel} className="absolute inset-y-0 left-0 flex w-64 max-w-[85vw] flex-col border-r border-border bg-bg shadow-lg">
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
  const hamburger = useRef<HTMLButtonElement>(null);
  const closeDrawer = useCallback(() => setDrawerOpen(false), []);
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

  if (me.data.must_change_password) {
    // a temporary password: the server refuses everything else until it is changed
    return (
      <div className="flex min-h-screen flex-col">
        <header className="flex h-14 items-center justify-between border-b border-border px-4 md:px-6">
          <Brand />
          <div className="flex items-center gap-3">
            <span className="hidden text-sm text-muted sm:inline">{me.data.email}</span>
            <Button variant="ghost" size="sm" onClick={onLogout} disabled={logout.isPending}>
              <LogOut />
              Esci
            </Button>
          </div>
        </header>
        <main className="mx-auto w-full max-w-md flex-1 p-4 md:p-6">
          <ChangePasswordSection forced />
        </main>
      </div>
    );
  }

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
      <MobileDrawer open={drawerOpen} onClose={closeDrawer} returnFocus={hamburger} />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-end gap-3 border-b border-border px-4 md:px-6">
          <Button
            ref={hamburger}
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
          {me.data.role === "viewer" && (
            <Badge variant="outline" title="Puoi vedere tutto; puoi solo disattivare il denaro reale e attivare il kill-switch">
              Sola lettura
            </Badge>
          )}
          <span className="hidden text-sm text-muted sm:inline">{me.data.email}</span>
          <Button variant="ghost" size="sm" onClick={onLogout} disabled={logout.isPending}>
            <LogOut />
            Esci
          </Button>
        </header>
        <NewVersionBanner />
        <ModeBanner />
        <PaperclipBanner />
        <main className="flex-1 overflow-auto p-4 md:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
