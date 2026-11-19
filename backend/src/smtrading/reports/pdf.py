"""The «Resoconto» PDF (fpdf2, pure Python): A4, Italian, from `reports.period` data.

The text is set in DejaVu Sans (regular and bold, embedded: `fonts/`, with its license), a
Unicode TrueType font, so €, − and the Italian punctuation render as they are; a character
the font has no glyph for (e.g. an emoji) becomes «?» (`_text`). Numbers in Italian format
(1.234,56, negatives with the minus sign −); «n.d.» when a value is not available."""

from datetime import date, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fpdf import FPDF
from fpdf.fonts import FontFace

FONT = "dejavu"
FONTS_DIR = Path(__file__).parent / "fonts"
_FONT_FILES = {"": "DejaVuSans.ttf", "B": "DejaVuSans-Bold.ttf"}
MINUS = "−"
NA = "n.d."
INK = (33, 37, 41)
MUTED = (108, 117, 125)
ACCENT = (13, 71, 161)
GRID = (222, 226, 230)
HEAD_BG = (233, 238, 246)
ZEBRA = (248, 249, 251)
SERIES_COLORS = {"total": ACCENT, "SPY": (230, 126, 34), "QQQ": (39, 174, 96),
                 "VWCE.DE": (142, 68, 173)}
KIND_LABELS = {"min": "Minimo", "cap": "Tetto", "max_loss": "Perdita massima",
               "beat_index": "Battere indice"}
HORIZON_LABELS = {"week": "settimana", "month": "mese", "quarter": "trimestre",
                  "year": "anno", "custom": "personalizzato"}
DECISION_LABELS = {"strategy": "Strategia", "risk_rule": "Regola di rischio",
                   "allocation": "Allocazione", "veto": "Veto", "lesson": "Lezione",
                   "team_rule": "Regola del team"}
STATUS_LABELS = {"bozza": "bozza", "attiva": "attiva", "conclusa": "conclusa"}
RULE_LABELS = {"active": "attiva", "retired": "ritirata"}


@cache
def _glyphs() -> frozenset[int]:
    """The code points both embedded fonts have a glyph for."""
    pdf = FPDF()
    for style, name in _FONT_FILES.items():
        pdf.add_font(FONT, style, str(FONTS_DIR / name))
    return frozenset.intersection(*(frozenset(f.cmap) for f in pdf.fonts.values()))


def _text(text: Any) -> str:
    s = "" if text is None else str(text)
    glyphs = _glyphs()
    return "".join(ch if ord(ch) in glyphs or ch in "\n\t" else "?" for ch in s)


def _num(v: Decimal | float | None, digits: int = 2) -> str:
    if v is None:
        return NA
    text = f"{Decimal(v):,.{digits}f}"
    text = text.replace(",", "_").replace(".", ",").replace("_", ".")
    return MINUS + text[1:] if text.startswith("-") else text


def money(v: Decimal | None, ccy: str = "EUR") -> str:
    return NA if v is None else f"{_num(v)} {'€' if ccy == 'EUR' else ccy}"


def signed_money(v: Decimal | None) -> str:
    return NA if v is None else ("+" if v > 0 else "") + money(v)


def pct(v: Decimal | None) -> str:
    return NA if v is None else ("+" if v > 0 else "") + _num(v) + "%"


def pp(v: Decimal | None) -> str:
    return NA if v is None else ("+" if v > 0 else "") + _num(v) + " pp"


def day(d: date | None) -> str:
    return NA if d is None else d.strftime("%d/%m/%Y")


def when(ts: datetime | None, zone: ZoneInfo) -> str:
    return NA if ts is None else ts.astimezone(zone).strftime("%d/%m/%Y %H:%M")


class _Doc(FPDF):
    footer_text = "SMTrading - Resoconto"

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font(FONT, size=7)
        self.set_text_color(*MUTED)
        self.cell(0, 5, _text(self.footer_text), align="L")
        self.set_x(self.l_margin)
        self.cell(0, 5, f"Pagina {self.page_no()} di {{nb}}", align="R")


class _Writer:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.zone = ZoneInfo(data.get("timezone") or "Europe/Rome")
        pdf = _Doc(orientation="P", unit="mm", format="A4")
        for style, name in _FONT_FILES.items():
            pdf.add_font(FONT, style, str(FONTS_DIR / name))
        pdf.set_margins(14, 14, 14)
        pdf.set_auto_page_break(True, margin=16)
        pdf.set_title(_text(data["title"]))
        pdf.set_author("SMTrading")
        pdf.set_creator("SMTrading")
        pdf.footer_text = f"SMTrading - {data['title']} - {day(data['from'])} - {day(data['to'])}"
        pdf.alias_nb_pages()
        self.pdf = pdf

    # -- primitives ------------------------------------------------------------------

    @property
    def width(self) -> float:
        return self.pdf.w - self.pdf.l_margin - self.pdf.r_margin

    def text(self, value: Any, size: float = 9, *, bold: bool = False,
             color=INK, h: float = 4.6) -> None:
        self.pdf.set_font(FONT, "B" if bold else "", size)
        self.pdf.set_text_color(*color)
        self.pdf.multi_cell(0, h, _text(value), new_x="LMARGIN", new_y="NEXT")

    def heading(self, title: str) -> None:
        if self.pdf.get_y() > self.pdf.h - 50:
            self.pdf.add_page()
        self.pdf.ln(3)
        self.pdf.set_font(FONT, "B", 12.5)
        self.pdf.set_text_color(*ACCENT)
        self.pdf.cell(0, 7, _text(title), new_x="LMARGIN", new_y="NEXT")
        y = self.pdf.get_y()
        self.pdf.set_draw_color(*ACCENT)
        self.pdf.set_line_width(0.4)
        self.pdf.line(self.pdf.l_margin, y, self.pdf.l_margin + self.width, y)
        self.pdf.ln(2)

    def empty(self, message: str) -> None:
        self.text(message, 8.5, color=MUTED)

    def table(self, headers: list[str], rows: list[list[Any]], widths: list[float],
              align: str | None = None, size: float = 7.5) -> None:
        """`align`: one letter per column (L, R, C)."""
        pdf = self.pdf
        pdf.set_font(FONT, "", size)
        pdf.set_text_color(*INK)
        pdf.set_draw_color(*GRID)
        pdf.set_line_width(0.2)
        aligns = tuple({"L": "LEFT", "R": "RIGHT", "C": "CENTER"}[a]
                       for a in (align or "L" * len(headers)))
        pdf.set_fill_color(255, 255, 255)
        head = FontFace(emphasis="BOLD", fill_color=HEAD_BG, color=INK)
        with pdf.table(col_widths=tuple(widths), text_align=aligns, headings_style=head,
                       cell_fill_color=ZEBRA, cell_fill_mode="ROWS",
                       line_height=size * 0.55, padding=1.2, width=self.width,
                       first_row_as_headings=True, repeat_headings=1,
                       borders_layout="HORIZONTAL_LINES") as t:
            r = t.row()
            for h in headers:
                r.cell(_text(h))
            for row in rows:
                r = t.row()
                for v in row:
                    r.cell(_text(v))
        pdf.ln(2)

    def kpis(self, items: list[tuple[str, str]]) -> None:
        pdf = self.pdf
        n = len(items)
        w = self.width / n
        x0, y0 = pdf.l_margin, pdf.get_y()
        for i, (label, value) in enumerate(items):
            x = x0 + i * w
            pdf.set_fill_color(*HEAD_BG)
            pdf.rect(x + 0.8, y0, w - 1.6, 15, style="F")
            pdf.set_xy(x + 2.5, y0 + 2)
            pdf.set_font(FONT, "", 7)
            pdf.set_text_color(*MUTED)
            pdf.cell(w - 5, 3.5, _text(label))
            pdf.set_xy(x + 2.5, y0 + 7)
            pdf.set_font(FONT, "B", 10.5)
            pdf.set_text_color(*INK)
            pdf.cell(w - 5, 5, _text(value))
        pdf.set_xy(x0, y0 + 18)

    # -- chart -----------------------------------------------------------------------

    def chart(self) -> None:
        curve = self.data["total"]["curve"]
        if len(curve) < 2:
            self.empty("Dati insufficienti per il grafico (servono almeno due giorni con "
                       "un valore).")
            return
        base = curve[0]["equity_eur"]
        if not base:
            self.empty("Grafico non disponibile: valore iniziale nullo.")
            return
        days = [c["day"] for c in curve]
        series = [("Portafogli (totale)", SERIES_COLORS["total"],
                   [(c["day"], (c["equity_eur"] / base - 1) * 100) for c in curve])]
        for b in self.data["benchmarks"]:
            pts = [(p["day"], p["pct"]) for p in b["series"] if p["pct"] is not None]
            if len(pts) >= 2:
                series.append((b["label"], SERIES_COLORS.get(b["symbol"], MUTED), pts))
        values = [float(v) for _, _, pts in series for _, v in pts]
        lo, hi = min(values + [0.0]), max(values + [0.0])
        if hi - lo < 1:
            hi, lo = hi + 0.5, lo - 0.5
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad
        pdf = self.pdf
        if pdf.get_y() > pdf.h - 90:
            pdf.add_page()
        x0, y0 = pdf.l_margin + 12, pdf.get_y() + 2
        w, h = self.width - 14, 62
        first, last = days[0].toordinal(), days[-1].toordinal()
        span = max(1, last - first)

        def px(d: date) -> float:
            return x0 + (d.toordinal() - first) / span * w

        def py(v: float) -> float:
            return y0 + h - (v - lo) / (hi - lo) * h

        pdf.set_line_width(0.15)
        pdf.set_draw_color(*GRID)
        pdf.set_font(FONT, "", 6.5)
        pdf.set_text_color(*MUTED)
        for i in range(5):
            v = lo + (hi - lo) * i / 4
            y = py(v)
            pdf.line(x0, y, x0 + w, y)
            pdf.set_xy(pdf.l_margin, y - 1.5)
            pdf.cell(11, 3, _text(f"{_num(Decimal(str(v)), 1)}%"), align="R")
        if lo < 0 < hi:
            pdf.set_draw_color(*MUTED)
            pdf.set_line_width(0.25)
            pdf.line(x0, py(0), x0 + w, py(0))
        for i, d in enumerate((days[0], days[len(days) // 2], days[-1])):
            pdf.set_xy(px(d) - 12, y0 + h + 1)
            pdf.cell(24, 3, day(d), align=("L", "C", "R")[i])
        for _, color, pts in series:
            pdf.set_draw_color(*color)
            pdf.set_line_width(0.55)
            pdf.polyline([(px(d), py(float(v))) for d, v in pts])
        # legend
        lx, ly = x0, y0 + h + 6
        pdf.set_font(FONT, "", 7)
        for label, color, _ in series:
            pdf.set_fill_color(*color)
            pdf.rect(lx, ly + 0.8, 4, 1.6, style="F")
            pdf.set_xy(lx + 5, ly)
            pdf.set_text_color(*INK)
            tw = pdf.get_string_width(_text(label)) + 8
            pdf.cell(tw, 3.4, _text(label))
            lx += tw + 4
        pdf.set_xy(pdf.l_margin, ly + 6)
        self.text("Variazione percentuale dal primo giorno del periodo; per i portafogli "
                  "depositi e prelievi non sono neutralizzati.", 7, color=MUTED, h=3.6)

    # -- sections --------------------------------------------------------------------

    def cover(self) -> None:
        d, pdf = self.data, self.pdf
        pdf.add_page()
        pdf.set_font(FONT, "B", 18)
        pdf.set_text_color(*ACCENT)
        pdf.multi_cell(0, 9, _text(d["title"]), new_x="LMARGIN", new_y="NEXT")
        self.text(f"Periodo: {day(d['from'])} - {day(d['to'])}", 10.5, bold=True)
        c = d.get("campaign")
        if c:
            self.text(f"Stato della campagna: {STATUS_LABELS.get(c['status'], c['status'])}"
                      + (f" - chiusa il {when(c['closed_at'], self.zone)}"
                         if c.get("closed_at") else ""), 9)
            if c.get("notes"):
                self.text(f"Note: {c['notes']}", 8.5, color=MUTED)
        names = ", ".join(p["name"] + (" (archiviato)" if p["archived"] else "")
                          for p in d["portfolios"]) or "nessuno"
        self.text(f"Portafogli: {names}", 9)
        self.text(f"Generato il {when(d['generated_at'], self.zone)} "
                  f"(ora di {d['timezone']})", 8, color=MUTED)

    def summary(self) -> None:
        d = self.data
        t = d["total"]
        self.heading("Sintesi")
        self.kpis([("Valore iniziale", money(t["start_eur"])),
                   ("Valore finale", money(t["end_eur"])),
                   ("Variazione", signed_money(t["change_eur"])),
                   ("Rendimento", pct(t["return_pct"]))])
        ops = d["operations"]
        self.kpis([("P&L realizzato (lordo)", signed_money(ops["realized_pnl_eur"])),
                   ("Commissioni del periodo", money(ops["fees_eur"])),
                   ("Operazioni chiuse", str(ops["trades_closed"])),
                   ("Costo agenti", f"{_num(d['agent_costs']['cost_usd'])} USD")])
        rows = [[b["label"], pct(b["change_pct"]), pp(b["diff_pp"]),
                 _verdict(b["diff_pp"])] for b in d["benchmarks"]]
        self.text("Confronto con gli indici di riferimento (in EUR)", 9, bold=True)
        self.table(["Indice", "Variazione indice", "Differenza portafogli", "Esito"], rows,
                   [30, 45, 50, 57], "LRRL")
        if all(b["change_pct"] is None for b in d["benchmarks"]):
            self.empty("Gli indici sono campionati solo per gli obiettivi «battere un "
                       "indice» attivi: nessun dato per questo periodo.")

    def portfolios(self) -> None:
        self.heading("Andamento")
        self.chart()
        self.heading("Rendimento per portafoglio")
        rows = [[p["name"] + (" (arch.)" if p["archived"] else ""),
                 money(p["start_eur"]), money(p["end_eur"]), signed_money(p["change_eur"]),
                 pct(p["return_pct"]), pct(p["max_drawdown_pct"]),
                 _num(p["sharpe"]) if p["sharpe"] is not None else NA]
                for p in self.data["portfolios"]]
        t = self.data["total"]
        rows.append(["Totale", money(t["start_eur"]), money(t["end_eur"]),
                     signed_money(t["change_eur"]), pct(t["return_pct"]),
                     pct(t["max_drawdown_pct"]), ""])
        self.table(["Portafoglio", "Inizio", "Fine", "Variazione", "Rend.", "Max DD",
                    "Sharpe"], rows, [40, 26, 26, 26, 18, 18, 28], "LRRRRRR")

    def operations(self) -> None:
        ops = self.data["operations"]
        self.heading("Operazioni")
        self.text(f"Esecuzioni nel periodo: {ops['fills']} - operazioni aperte: "
                  f"{ops['trades_opened']} - chiuse: {ops['trades_closed']} - P&L "
                  f"realizzato {signed_money(ops['realized_pnl_eur'])}, costi "
                  f"{money(ops['costs_eur'])}, netto {signed_money(ops['net_pnl_eur'])}.", 8.5)
        if not ops["trades"]:
            self.empty("Nessuna operazione nel periodo.")
            return
        rows = []
        for t in ops["trades"]:
            rows.append([
                t["portfolio"], t["symbol"], "Long" if t["direction"] == "long" else "Short",
                when(t["opened_at"], self.zone)[:10],
                when(t["closed_at"], self.zone)[:10] if t["closed_at"] else "aperta",
                _num(t["qty"], 4).rstrip("0").rstrip(","),
                _num(t["entry_price"], 4), _num(t["exit_price"], 4)
                if t["exit_price"] is not None else NA,
                signed_money(t["realized_pnl_eur"]), money(t["costs_eur"])])
        self.table(["Portafoglio", "Strumento", "Dir.", "Apertura", "Chiusura", "Qtà",
                    "Entrata", "Uscita", "P&L realizz.", "Costi"], rows,
                   [24, 18, 11, 17, 17, 13, 18, 18, 24, 22], "LLLLLRRRRR", size=6.8)
        if ops["trades_total"] > len(ops["trades"]):
            self.empty(f"Mostrate le {len(ops['trades'])} operazioni più recenti su "
                       f"{ops['trades_total']}.")
        self.empty("P&L lordo in EUR al cambio attuale; i costi sono commissioni e costi "
                   "di cambio.")

    def strategies(self) -> None:
        self.heading("Risultati per strategia")
        items = self.data["strategies"]
        if not items:
            self.empty("Nessuna strategia con operazioni nel periodo.")
            return
        rows = [[(s.ref + (f" {s.name}" if s.name else "")) if s.ref else "Senza strategia",
                 s.status or "", str(s.trades_opened), str(s.trades_closed),
                 pct(s.win_rate_pct).lstrip("+") if s.win_rate_pct is not None else NA,
                 signed_money(s.realized_pnl_eur), money(s.costs_eur),
                 signed_money(s.net_pnl_eur)] for s in items]
        self.table(["Strategia", "Stato", "Aperte", "Chiuse", "Vincenti", "P&L realizz.",
                    "Costi", "Netto"], rows, [46, 18, 14, 14, 17, 26, 22, 25], "LLRRRRRR")

    def goals(self) -> None:
        self.heading("Obiettivi")
        items = self.data["goals"]
        if not items:
            self.empty("Nessun obiettivo dei portafogli in questo periodo.")
            return
        rows = []
        for g in items:
            target = (f"+{_num(g['target_pct'])} pp su {g['benchmark']}"
                      if g["kind"] == "beat_index"
                      else f"{MINUS}{_num(g['target_pct'])}%" if g["kind"] == "max_loss"
                      else f"{_num(g['target_pct'])}%")
            name = g["name"] + (" (piano)" if g.get("closes_positions") else "")
            rows.append([name, g.get("portfolio_name") or "totale",
                         KIND_LABELS.get(g["kind"], g["kind"]),
                         f"{day(g['period_start'])} - {day(g['period_end'])}", target,
                         pct(g["pct"]), g["status"]])
        self.table(["Obiettivo", "Portafoglio", "Tipo", "Periodo", "Obiettivo %",
                    "Risultato", "Esito"], rows, [34, 28, 24, 36, 22, 18, 20], "LLLLRRL")

    def costs(self) -> None:
        c = self.data["agent_costs"]
        self.heading("Costi degli agenti")
        self.text(f"Totale del periodo: {_num(c['cost_usd'])} USD (a consumo "
                  f"{_num(c['billed_usd'])} USD), {c['runs']} esecuzioni. Costi dell'intero "
                  "team, non ripartiti per portafoglio.", 8.5)
        if c["agents"]:
            self.table(["Agente", "Costo (USD)", "A consumo (USD)", "Esecuzioni"],
                       [[a["agent"], _num(a["cost_usd"]), _num(a["billed_usd"]),
                         str(a["runs"])] for a in c["agents"]], [80, 35, 35, 32], "LRRR")

    def lessons(self) -> None:
        self.heading("Lezioni e regole del team")
        lessons, rules = self.data["lessons"], self.data["team_rules"]
        if not lessons and not rules:
            self.empty("Nessuna lezione o regola registrata nel periodo.")
            return
        rows = [[when(x["created_at"], self.zone)[:10], DECISION_LABELS[x["kind"]]
                 + (f" ({RULE_LABELS.get(x['status'], x['status'])})" if x.get("status") else ""), x["agent"] or "",
                 x["summary"]] for x in rules + lessons]
        self.table(["Data", "Tipo", "Agente", "Testo"], rows, [18, 30, 30, 104], "LLLL")

    def decisions(self) -> None:
        self.heading("Decisioni rilevanti")
        items = self.data["decisions"]
        if not items:
            self.empty("Nessuna decisione rilevante nel periodo.")
            return
        rows = [[when(x["created_at"], self.zone)[:10], DECISION_LABELS.get(x["kind"],
                                                                              x["kind"]),
                 x["agent"] or "", x["portfolio"] or "team",
                 x["summary"] + (f" - {x['reason']}" if x.get("reason") else "")]
                for x in items]
        self.table(["Data", "Tipo", "Agente", "Portafoglio", "Decisione"], rows,
                   [18, 26, 28, 26, 84], "LLLLL")

    def render(self) -> bytes:
        self.cover()
        self.summary()
        self.portfolios()
        self.operations()
        self.strategies()
        self.goals()
        self.costs()
        self.lessons()
        self.decisions()
        return bytes(self.pdf.output())


def _verdict(diff: Decimal | None) -> str:
    if diff is None:
        return "dati non disponibili"
    if diff > 0:
        return "meglio dell'indice"
    if diff < 0:
        return "peggio dell'indice"
    return "in linea"


def render_pdf(data: dict[str, Any]) -> bytes:
    """The PDF bytes of a `reports.period.period_report` result."""
    return _Writer(data).render()


def filename(data: dict[str, Any]) -> str:
    base = "resoconto"
    if data.get("campaign"):
        slug = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in data["campaign"]["name"].lower())
        base += "-" + "-".join(p for p in slug.split("-") if p)[:60]
    return f"{base}-{data['from'].isoformat()}-{data['to'].isoformat()}.pdf"
