import {
  Bot,
  Cable,
  FileText,
  Gauge,
  Globe,
  Landmark,
  LayoutDashboard,
  Percent,
  Settings,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import { NavLink } from "react-router-dom";
import { cn } from "@/lib/utils";

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard },
  { to: "/portafogli", label: "Portafogli", icon: Wallet },
  { to: "/agenti", label: "Agenti", icon: Bot },
  { to: "/commissioni", label: "Commissioni", icon: Percent },
  { to: "/rischio", label: "Rischio e margine", icon: Gauge },
  { to: "/connettori", label: "Connettori", icon: Cable },
  { to: "/broker", label: "Broker", icon: Landmark },
  { to: "/mercati", label: "Mercati", icon: Globe },
  { to: "/log-mcp", label: "Log MCP", icon: FileText },
  { to: "/impostazioni", label: "Impostazioni", icon: Settings },
];

export function Sidebar() {
  return (
    <nav aria-label="Navigazione principale" className="flex flex-col gap-1 p-3">
      {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          end={to === "/"}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-3 rounded-md px-3 py-2 text-sm text-muted transition-colors hover:bg-card hover:text-fg",
              isActive && "bg-card font-medium text-fg",
            )
          }
        >
          <Icon className="h-4 w-4" aria-hidden />
          {label}
        </NavLink>
      ))}
    </nav>
  );
}
