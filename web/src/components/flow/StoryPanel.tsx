import { useRef, type ReactNode } from "react";
import { ApiError } from "@/api/client";
import { useFlowMcpStory, useFlowOrderStory } from "@/api/hooks/flow";
import type { FlowMcpStory, FlowOrderStory } from "@/api/types";
import { formatPct, formatQty } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { StoryTarget } from "./replay";
import { BTN, clock, H4, PANEL } from "./styles";
import { useDialog } from "./useDialog";

/** «Storia di un ordine»: the side panel opened by a comet or a diary line — the whole
 * journey of an order (GET /api/flow/order/{id}) or, for an agent's place_* call, the call
 * itself (GET /api/flow/mcp/{id}: a refused call creates no order). Telegram: category
 * and time only, never the text. */

const SIDE_IT: Record<string, string> = { buy: "acquisto", sell: "vendita" };
const TYPE_IT: Record<string, string> = { market: "a mercato", limit: "limite", stop: "stop", stop_limit: "stop limite" };

const qty = (v: string | null | undefined) => (v == null ? "—" : formatQty(v));

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="border-t border-white/10 pt-2.5">
      <h4 className={H4}>{title}</h4>
      <div className="space-y-1 text-[12.5px] text-[#cfd8f5]">{children}</div>
    </section>
  );
}

function Note({ children }: { children: ReactNode }) {
  return <div className="italic text-[#8b99c3]">{children}</div>;
}

/** an agent's `expected` (free JSON): "chiave valore" pairs, numbers in Italian */
function expectedText(v: unknown): string | null {
  if (v == null) return null;
  if (typeof v !== "object") return String(v);
  const parts = Object.entries(v as Record<string, unknown>).map(([k, x]) => {
    const val = typeof x === "number" ? x.toLocaleString("it-IT", { maximumFractionDigits: 4 }) : typeof x === "object" ? JSON.stringify(x) : String(x);
    return `${k} ${val}`;
  });
  return parts.length ? parts.join(" · ") : null;
}

function OrderStory({ s }: { s: FlowOrderStory }) {
  const o = s.order;
  const expected = s.decision ? expectedText(s.decision.expected) : null;
  return (
    <>
      <div className="text-[15px] font-semibold text-[#e8eeff]">
        {SIDE_IT[o.side] ?? o.side} {o.symbol} · {o.qty != null ? qty(o.qty) : o.notional != null ? `${qty(o.notional)} di controvalore` : "—"}
      </div>
      <div className="text-xs text-[#8b99c3]">
        {TYPE_IT[o.type] ?? o.type}
        {o.limit_price ? ` a ${qty(o.limit_price)}` : ""}
        {o.stop_price ? ` · stop ${qty(o.stop_price)}` : ""} · {o.status_it}
        {s.agent ? ` · ${s.agent.name}` : ""}
        {o.broker ? ` · ${o.broker.label}` : ""} · creato alle {clock(o.created_at)}
      </div>

      <Section title="Decisione">
        {s.decision ? (
          <>
            {s.decision.summary && <div className="font-semibold text-[#e8eeff]">{s.decision.summary}</div>}
            {s.decision.reason && <div>motivo: {s.decision.reason}</div>}
            <div className="text-[#8b99c3]">
              {s.decision.confidence != null ? `fiducia ${s.decision.confidence}/100` : "fiducia non indicata"}
              {expected ? ` · atteso: ${expected}` : ""} · {clock(s.decision.at)}
            </div>
          </>
        ) : (
          <Note>{s.decision_note ?? "decisione non collegata"}</Note>
        )}
      </Section>

      <Section title="Chiamata MCP">
        {s.mcp ? (
          <div>
            <span className="font-mono text-[#cdb8ff]">{s.mcp.tool}</span> · {s.mcp.outcome === "ok" ? "riuscita" : `non riuscita${s.mcp.reason_code ? ` (${s.mcp.reason_code})` : ""}`}
            {s.mcp.latency_ms != null ? ` · ${s.mcp.latency_ms} ms` : ""} · {clock(s.mcp.at)}
          </div>
        ) : (
          <Note>{s.mcp_note ?? "chiamata MCP non trovata"}</Note>
        )}
      </Section>

      <Section title="Percorso">
        {s.steps.length === 0 && <Note>nessun passaggio registrato</Note>}
        <ol className="space-y-0.5">
          {s.steps.map((st, i) => (
            <li key={`${st.step}-${i}`} className={cn("font-mono text-xs", st.step === "rejected" && "text-[#ff6f8c]")}>
              <span className="text-[#8b99c3]">{clock(st.at)}</span> {st.label}
            </li>
          ))}
        </ol>
      </Section>

      {s.rejected && (
        <Section title="Rifiuto">
          <div className="text-[#ff9fb2]">
            {s.rejected.code ?? "rifiutato"}
            {s.rejected.message ? ` — ${s.rejected.message}` : ""} · {clock(s.rejected.at)}
          </div>
        </Section>
      )}

      <Section title="Ordini collegati">
        {s.children.length === 0 ? (
          <Note>nessuno</Note>
        ) : (
          s.children.map((c) => (
            <div key={c.id}>
              {c.role_it || "ordine collegato"} · {TYPE_IT[c.type] ?? c.type}
              {c.stop_price ? ` ${qty(c.stop_price)}` : ""}
              {c.limit_price ? ` ${qty(c.limit_price)}` : ""} · {c.status_it}
            </div>
          ))
        )}
      </Section>

      <Section title="Esecuzioni">
        {s.fills.length === 0 ? (
          <Note>nessuna esecuzione</Note>
        ) : (
          s.fills.map((f) => (
            <div key={f.id}>
              {qty(f.qty)} a {qty(f.price)}
              {f.expected_price ? ` · atteso ${qty(f.expected_price)}${f.expected_from ? ` (${f.expected_from})` : ""}` : " · prezzo atteso non noto"}
              {f.slippage_pct ? ` · scarto ${formatPct(f.slippage_pct)}` : ""} · {f.source === "sim" ? "simulata" : "dal broker"} · {clock(f.at)}
            </div>
          ))
        )}
      </Section>

      <Section title="Telegram">
        {s.telegram.length === 0 ? (
          <Note>nessun avviso</Note>
        ) : (
          s.telegram.map((t, i) => (
            <div key={i}>
              {t.category_it} · {clock(t.at)}
            </div>
          ))
        )}
      </Section>

      <Section title="Esito">
        <div className="font-semibold text-[#e8eeff]">
          {s.outcome.status_it}
          {s.outcome.filled_qty && Number(s.outcome.filled_qty) > 0
            ? ` · eseguiti ${qty(s.outcome.filled_qty)}${s.outcome.avg_fill_price ? ` a ${qty(s.outcome.avg_fill_price)} di media` : ""}`
            : ""}
        </div>
      </Section>
    </>
  );
}

/** The backend's note for an order tool refused by the checks. The MCP story has no
 * structured «refused» field (only outcome "error" + reason_code), so the refusal is read
 * from this note, which the backend sets for order tools only. */
const REFUSED = "chiamata rifiutata";
/** the backend's order tools (flow/story.py ORDER_TOOLS): an accepted one should have
 * created an order */
const ORDER_TOOLS = new Set(["place_order", "place_bracket_order", "place_oco_order", "close_position", "close_all_positions", "modify_order"]);

function McpStory({ s, onOpen }: { s: FlowMcpStory; onOpen?: (t: StoryTarget) => void }) {
  const refused = s.note?.startsWith(REFUSED) ?? false;
  return (
    <>
      <div className="text-[15px] font-semibold text-[#e8eeff]">
        <span className="font-mono text-[#cdb8ff]">{s.tool}</span>
        {s.symbol ? ` su ${s.symbol}` : ""}
      </div>
      <div className="text-xs text-[#8b99c3]">
        {s.agent.name} · {clock(s.at)}
      </div>
      <Section title="Chiamata MCP">
        <div className={s.outcome === "ok" ? undefined : "text-[#ff9fb2]"}>
          {s.outcome === "ok" ? "riuscita" : refused ? "rifiutata" : "non riuscita"}
          {s.reason_code ? ` · motivo ${s.reason_code}` : ""}
          {s.latency_ms != null ? ` · ${s.latency_ms} ms` : ""}
        </div>
        {s.note && <Note>{s.note}</Note>}
        {s.order_id ? (
          <button type="button" className={cn(BTN, "mt-1")} onClick={() => onOpen?.({ kind: "order", id: s.order_id! })}>
            apri la storia dell&apos;ordine
          </button>
        ) : (
          s.outcome === "ok" && ORDER_TOOLS.has(s.tool) && <Note>ordine non trovato per questa chiamata</Note>
        )}
      </Section>
    </>
  );
}

const NOT_FOUND = {
  order: "storia non disponibile: ordine non trovato o di un portafoglio nascosto",
  mcp: "storia non disponibile: chiamata non trovata",
};
const TITLE = { order: "Storia dell'ordine", mcp: "Chiamata MCP" };

export function StoryPanel({ target, onClose, onOpen }: { target: StoryTarget; onClose: () => void; onOpen?: (t: StoryTarget) => void }) {
  const order = useFlowOrderStory(target.kind === "order" ? target.id : null);
  const mcp = useFlowMcpStory(target.kind === "mcp" ? target.id : null);
  const dialog = useRef<HTMLDivElement>(null);
  useDialog(dialog, onClose);
  const q = target.kind === "order" ? order : mcp;
  let body: ReactNode;
  if (q.isError) {
    const missing = q.error instanceof ApiError && q.error.status === 404;
    body = <Note>{missing ? NOT_FOUND[target.kind] : "storia non leggibile, riprova"}</Note>;
  } else if (!q.data) body = <Note>caricamento…</Note>;
  else body = target.kind === "order" ? <OrderStory s={q.data as FlowOrderStory} /> : <McpStory s={q.data as FlowMcpStory} onOpen={onOpen} />;
  return (
    <div
      ref={dialog}
      tabIndex={-1}
      role="dialog"
      aria-label={TITLE[target.kind]}
      className={cn(PANEL, "absolute bottom-3 right-3 top-3 z-20 flex w-[min(380px,calc(100%-24px))] flex-col overflow-hidden outline-none")}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-center gap-2 px-4 pb-1 pt-3">
        <h3 className="m-0 flex-1 text-[13px] font-semibold uppercase tracking-[.14em] text-[#9ff3df]">{TITLE[target.kind]}</h3>
        <button
          type="button"
          aria-label="Chiudi la storia"
          className="rounded-md px-2 py-0.5 text-lg leading-none text-[#8b99c3] hover:bg-white/10 hover:text-[#e8eeff] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[#34e0c4]"
          onClick={onClose}
        >
          ×
        </button>
      </div>
      <div className="flex-1 space-y-2.5 overflow-y-auto px-4 pb-4 pt-1">{body}</div>
    </div>
  );
}
