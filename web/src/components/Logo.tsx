import { cn } from "@/lib/utils";

interface LogoProps {
  className?: string;
  /** standalone use: gives the mark an accessible name; omitted → decorative (aria-hidden) */
  label?: string;
}

/**
 * SMTrading mark: three rising candlesticks under an up-trend arrow.
 * The tile takes the theme accent via currentColor (same geometry as public/favicon.svg).
 */
export function Logo({ className, label }: LogoProps) {
  return (
    <svg
      viewBox="0 0 32 32"
      className={cn("h-7 w-7 shrink-0 text-accent", className)}
      data-testid="app-logo"
      {...(label ? { role: "img", "aria-label": label } : { "aria-hidden": true, focusable: false })}
    >
      <rect width="32" height="32" rx="7" fill="currentColor" />
      <g fill="#fff" stroke="#fff" strokeWidth="1.5" strokeLinecap="round">
        <path d="M8 19v8M16 15v10M24 11v12" fill="none" />
        <rect x="5.5" y="21" width="5" height="5" rx="1" stroke="none" />
        <rect x="13.5" y="17" width="5" height="6" rx="1" stroke="none" />
        <rect x="21.5" y="13" width="5" height="8" rx="1" stroke="none" />
      </g>
      <path
        d="M4.5 14.5 12.5 9.5 17.5 11.5 26 4.5M21 4h6v6"
        fill="none"
        stroke="#fff"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
