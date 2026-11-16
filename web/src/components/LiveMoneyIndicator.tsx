import { Link } from "react-router-dom";
import { useLiveStatus } from "@/api/hooks/live";
import { liveBannerText } from "@/lib/live";

/**
 * Header badge while a real-money link is armed: a red dot + «Reale». The tooltip says on
 * how many (and which) portfolios; a click opens the Broker page to disarm it.
 */
export function LiveMoneyIndicator() {
  const { data: live } = useLiveStatus();
  if (!live || live.armed_count <= 0) return null;
  const armed = live.portfolios.filter((p) => p.live_state === "armed");
  const names = armed.map((p) => p.name).join(", ");
  const title = `${liveBannerText(armed.length || live.armed_count)}${names ? `: ${names}` : ""} · clic per disattivare`;
  return (
    <Link
      to="/broker#denaro-reale"
      title={title}
      aria-label={title}
      className="inline-flex items-center gap-1.5 rounded-md border border-loss/50 px-2 py-0.5 text-xs font-semibold text-loss hover:bg-loss/10"
    >
      <span data-testid="live-money-dot" aria-hidden="true" className="inline-block h-2 w-2 rounded-full bg-loss" />
      Reale
    </Link>
  );
}
