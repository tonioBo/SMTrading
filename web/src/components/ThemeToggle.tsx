import { Monitor, Moon, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";
import { nextTheme, THEME_LABELS, useTheme } from "@/theme";

const ICON = { dark: Moon, light: Sun, auto: Monitor } as const;

/** One button: scuro → chiaro → automatico (follows the system) → scuro. */
export function ThemeToggle() {
  const { theme, cycle } = useTheme();
  const Icon = ICON[theme];
  const label = `Tema ${THEME_LABELS[theme]}: passa a ${THEME_LABELS[nextTheme(theme)]}`;
  return (
    <Button variant="ghost" size="icon" onClick={cycle} aria-label={label} title={label}>
      <Icon />
    </Button>
  );
}
