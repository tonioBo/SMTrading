/** The «Fiume dei flussi» look (the approved sketch's dark glass), shared by the page and
 * its v2 panels (replay bar, order story, lens). */
export const GLASS =
  "rounded-2xl border border-[rgba(150,170,255,.15)] bg-[rgba(9,12,26,.6)] shadow-[0_16px_46px_rgba(0,0,0,.4)] backdrop-blur-md";
/** a panel over the river: more opaque, so its text reads over the comets */
export const PANEL =
  "rounded-2xl border border-[rgba(150,170,255,.2)] bg-[rgba(9,12,26,.88)] shadow-[0_16px_46px_rgba(0,0,0,.5)] backdrop-blur-md";
export const BTN =
  "whitespace-nowrap rounded-[9px] border border-white/15 bg-white/[.06] px-2.5 py-1.5 text-xs font-semibold text-[#e8eeff] hover:bg-white/[.12] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#34e0c4] disabled:opacity-40";
export const BTN_ON = "border-[#34e0c4] bg-[rgba(52,224,196,.18)] text-[#bff7ec] hover:bg-[rgba(52,224,196,.24)]";
export const H4 = "mb-1.5 text-[10.5px] font-semibold uppercase tracking-[.16em] text-[#6f80b3]";

const CLOCK = new Intl.DateTimeFormat("it-IT", { timeZone: "Europe/Rome", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
const DAY = new Intl.DateTimeFormat("it-IT", { timeZone: "Europe/Rome", day: "2-digit", month: "2-digit" });

/** "HH:mm:ss" in Rome */
export const clock = (t: string | number) => CLOCK.format(new Date(t));
/** "dd/MM HH:mm:ss" in Rome */
export const dayClock = (t: string | number) => `${DAY.format(new Date(t))} ${CLOCK.format(new Date(t))}`;

export const num = (n: number, digits = 1) => n.toLocaleString("it-IT", { maximumFractionDigits: digits });
