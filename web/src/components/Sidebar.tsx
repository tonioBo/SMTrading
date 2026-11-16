import {
  Bell,
  BookOpen,
  Bot,
  Cable,
  CircleDollarSign,
  FileText,
  Gauge,
  Globe,
  Landmark,
  LayoutDashboard,
  Percent,
  Radar,
  ScrollText,
  Settings,
  ShieldAlert,
  Target,
  Wallet,
  type LucideIcon,
} from "lucide-react";
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

const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard },
  { to: "/portafogli", label: "Portafogli", icon: Wallet },
  { to: "/segnali", label: "Segnali", icon: Radar },
  { to: "/strategie", label: "Strategie", icon: Target },
  { to: "/agenti", label: "Agenti", icon: Bot },
  { to: "/costi-agenti", label: "Costi agenti", icon: CircleDollarSign },
  { to: "/istruzioni-agenti", label: "Istruzioni agenti", icon: BookOpen },
  { to: "/commissioni", label: "Commissioni", icon: Percent },
  { to: "/rischio", label: "Rischio e margine", icon: Gauge },
  { to: "/connettori", label: "Connettori", icon: Cable },
  { to: "/broker", label: "Broker", icon: Landmark },
  { to: "/registro-denaro-reale", label: "Registro denaro reale", icon: ShieldAlert },
  { to: "/notifiche", label: "Notifiche", icon: Bell },
  { to: "/mercati", label: "Mercati", icon: Globe },
  { to: "/log-mcp", label: "Log MCP", icon: FileText },
  { to: "/registro-attivita", label: "Registro attività", icon: ScrollText, adminOnly: true },
  { to: "/impostazioni", label: "Impostazioni", icon: Settings },
];

interface SidebarProps {
  /** icon-only rail: the label stays as the accessible name and the tooltip (title) */
  collapsed?: boolean;
  label?: string;
}

export function Sidebar({ collapsed = false, label = "Navigazione principale" }: SidebarProps) {
  const admin = useIsAdmin();
  return (
    <nav aria-label={label} className={cn("flex flex-col gap-1", collapsed ? "p-2" : "p-3")}>
      {NAV_ITEMS.filter((item) => admin || !item.adminOnly).map(({ to, label: text, icon: Icon }) => (
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
    </nav>
  );
}
