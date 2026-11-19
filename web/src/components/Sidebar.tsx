import {
  Bell,
  ChevronDown,
  BookOpen,
  Bot,
  Cable,
  CircleDollarSign,
  FileText,
  Flag,
  FlaskConical,
  Gauge,
  Globe,
  Landmark,
  Lightbulb,
  Layers,
  LayoutDashboard,
  Percent,
  Radar,
  ScrollText,
  Settings,
  ShieldAlert,
  Target,
  Trophy,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import { useId, useState } from "react";
import { NavLink } from "react-router-dom";
import { useIsAdmin } from "@/api/hooks/auth";
import { cn } from "@/lib/utils";

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** hidden from a «Sola lettura» user */
  adminOnly?: boolean;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

const NAV_GROUPS: NavGroup[] = [
  {
    label: "Trading",
    items: [
      { to: "/", label: "Dashboard", icon: LayoutDashboard },
      { to: "/portafogli", label: "Portafogli", icon: Wallet },
      { to: "/segnali", label: "Segnali", icon: Radar },
      { to: "/posizioni", label: "Posizioni", icon: Layers },
      { to: "/strategie", label: "Strategie", icon: Target },
      { to: "/obiettivi", label: "Obiettivi", icon: Flag },
      { to: "/campagne", label: "Campagne", icon: FlaskConical },
      { to: "/mercati", label: "Mercati", icon: Globe },
    ],
  },
  {
    label: "Agenti",
    items: [
      { to: "/agenti", label: "Agenti", icon: Bot },
      { to: "/istruzioni-agenti", label: "Istruzioni agenti", icon: BookOpen },
      { to: "/costi-agenti", label: "Costi agenti", icon: CircleDollarSign },
      { to: "/risultati-agenti", label: "Risultati agenti", icon: Trophy },
      { to: "/notifiche", label: "Notifiche", icon: Bell },
      { to: "/decisioni", label: "Decisioni e lezioni", icon: Lightbulb },
      { to: "/log-mcp", label: "Log MCP", icon: FileText },
    ],
  },
  {
    label: "Sistema",
    items: [
      { to: "/broker", label: "Broker", icon: Landmark },
      { to: "/connettori", label: "Connettori", icon: Cable },
      { to: "/commissioni", label: "Commissioni", icon: Percent },
      { to: "/rischio", label: "Rischio e margine", icon: Gauge },
      { to: "/registro-attivita", label: "Registro attività", icon: ScrollText, adminOnly: true },
      { to: "/registro-denaro-reale", label: "Registro denaro reale", icon: ShieldAlert },
      { to: "/impostazioni", label: "Impostazioni", icon: Settings },
    ],
  },
];

interface SidebarProps {
  /** icon-only rail: the label stays as the accessible name and the tooltip (title) */
  collapsed?: boolean;
  label?: string;
  /** small screens (the drawer): each group opens and closes with its heading */
  collapsibleGroups?: boolean;
}

function NavItems({ items, collapsed }: { items: NavItem[]; collapsed: boolean }) {
  return (
    <>
      {items.map(({ to, label: text, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          end={to === "/"}
          title={collapsed ? text : undefined}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-3 rounded-md py-2 text-sm text-muted transition-colors hover:bg-card hover:text-fg",
              collapsed ? "justify-center px-2" : "px-3",
              isActive && "bg-card font-medium text-fg",
            )
          }
        >
          <Icon className="h-4 w-4 shrink-0" aria-hidden />
          <span className={cn(collapsed && "sr-only")}>{text}</span>
        </NavLink>
      ))}
    </>
  );
}

function Group({ group, collapsed, collapsible, first }: { group: NavGroup; collapsed: boolean; collapsible: boolean; first: boolean }) {
  const [open, setOpen] = useState(true);
  const id = useId();
  if (collapsed) {
    // icon rail: a thin separator instead of the heading
    return (
      <div role="group" aria-label={group.label} className={cn("flex flex-col gap-1", !first && "mt-2 border-t border-border pt-2")}>
        <NavItems items={group.items} collapsed />
      </div>
    );
  }
  const heading = "px-3 text-xs font-semibold uppercase tracking-wide text-muted";
  return (
    <div role="group" aria-label={group.label} className={cn("flex flex-col gap-1", !first && "mt-3")}>
      {collapsible ? (
        <button
          type="button"
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen((v) => !v)}
          className={cn(heading, "flex items-center justify-between py-1 hover:text-fg")}
        >
          {group.label}
          <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", !open && "-rotate-90")} aria-hidden />
        </button>
      ) : (
        <div className={cn(heading, "py-1")} aria-hidden>
          {group.label}
        </div>
      )}
      <div id={id} hidden={!open} className="flex flex-col gap-1">
        <NavItems items={group.items} collapsed={false} />
      </div>
    </div>
  );
}

export function Sidebar({ collapsed = false, label = "Navigazione principale", collapsibleGroups = false }: SidebarProps) {
  const admin = useIsAdmin();
  return (
    <nav aria-label={label} className={cn("flex min-h-0 flex-1 flex-col overflow-y-auto", collapsed ? "p-2" : "p-3")}>
      {NAV_GROUPS.map((g, i) => (
        <Group
          key={g.label}
          group={{ ...g, items: g.items.filter((item) => admin || !item.adminOnly) }}
          collapsed={collapsed}
          collapsible={collapsibleGroups}
          first={i === 0}
        />
      ))}
    </nav>
  );
}
