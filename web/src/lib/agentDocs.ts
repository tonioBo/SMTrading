/** «Istruzioni agenti»: the roster (order, sections, tags) and the text to paste in Paperclip. */

export type TagKind = "plain" | "trade" | "cron";

export interface RosterEntry {
  slug: string;
  section: "setup" | "agents";
  /** shown instead of the title for the setup documents */
  label?: string;
  tags: { text: string; kind: TagKind }[];
}

const read = { text: "read", kind: "plain" } as const;
const onTask = { text: "su task", kind: "plain" } as const;

export const ROSTER: RosterEntry[] = [
  { slug: "readme", section: "setup", label: "Guida all'installazione", tags: [] },
  { slug: "common", section: "setup", label: "00 · Regole comuni", tags: [] },
  { slug: "ceo", section: "agents", tags: [{ text: "read + halt", kind: "plain" }, { text: "venerdì", kind: "cron" }] },
  { slug: "portfolio-manager", section: "agents", tags: [read, { text: "ogni ora", kind: "cron" }] },
  { slug: "risk-manager", section: "agents", tags: [read, { text: "ogni ora", kind: "cron" }] },
  { slug: "execution-supervisor", section: "agents", tags: [{ text: "trade", kind: "trade" }, onTask] },
  { slug: "market-analyst", section: "agents", tags: [read, onTask] },
  { slug: "news-analyst", section: "agents", tags: [read, { text: "08:30 + ogni 2h", kind: "cron" }] },
  { slug: "quant-researcher", section: "agents", tags: [read, onTask] },
  { slug: "performance-auditor", section: "agents", tags: [read, onTask] },
  { slug: "notifier", section: "agents", tags: [{ text: "read + notify", kind: "plain" }, onTask] },
];

export const COMMON_SLUG = "common";

export function isSetupDoc(slug: string): boolean {
  return ROSTER.some((r) => r.slug === slug && r.section === "setup");
}

/** Roster entries for the documents the server has, in roster order; unknown slugs last. */
export function rosterFor(slugs: string[]): RosterEntry[] {
  const known = ROSTER.filter((r) => slugs.includes(r.slug));
  const extra = slugs
    .filter((s) => !ROSTER.some((r) => r.slug === s))
    .map((slug): RosterEntry => ({ slug, section: "agents", tags: [] }));
  return [...known, ...extra];
}

/** What goes in the agent's instructions in Paperclip: the common rules, then the role. */
export function fullInstructions(common: string, role: string): string {
  return `${common.trim()}\n\n---\n\n${role.trim()}\n`;
}

/** Copies to the clipboard; false when the browser refuses (the caller shows a fallback). */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (!navigator.clipboard?.writeText) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
