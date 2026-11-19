"""The «Resoconto» PDF (reports.pdf): a smoke test on report data, Italian titles in the
extracted text, Italian number formats, characters outside Latin-1 replaced."""

import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

from pypdf import PdfReader

from smtrading.performance.agent_results import StrategyResult
from smtrading.reports import pdf


def _data(**over) -> dict:
    days = [date(2027, 3, 1) + timedelta(days=i) for i in range(10)]
    data = {
        "title": "Resoconto campagna «Prova di marzo»",
        "campaign": {"name": "Prova di marzo", "status": "conclusa",
                     "closed_at": datetime(2027, 3, 12, 17, tzinfo=UTC),
                     "notes": "Test con € e − segni"},
        "from": days[0], "to": days[-1], "generated_at": datetime(2027, 3, 12, tzinfo=UTC),
        "timezone": "Europe/Rome",
        "portfolios": [{"portfolio_id": "p1", "name": "Alfa", "base_currency": "EUR",
                        "archived": True, "start_eur": D(10000), "end_eur": D("10250.5"),
                        "change_eur": D("250.5"), "return_pct": D("2.505"),
                        "max_drawdown_pct": D("-1.2"), "sharpe": D("1.5")}],
        "total": {"start_eur": D(10000), "end_eur": D("10250.5"), "change_eur": D("250.5"),
                  "return_pct": D("2.505"), "max_drawdown_pct": D("-1.2"),
                  "curve": [{"day": d, "equity_eur": D(10000 + 25 * i)}
                            for i, d in enumerate(days)]},
        "benchmarks": [
            {"symbol": "SPY", "label": "SPY", "change_pct": D("1.10"), "diff_pp": D("1.41"),
             "series": [{"day": d, "pct": D(i) / 10} for i, d in enumerate(days)]},
            {"symbol": "QQQ", "label": "QQQ", "change_pct": None, "diff_pp": None,
             "series": []},
            {"symbol": "VWCE.DE", "label": "VWCE", "change_pct": D(3), "diff_pp": D("-0.5"),
             "series": []}],
        "operations": {"fills": 4, "fees_eur": D("3.2"), "trades_opened": 2,
                       "trades_closed": 1, "realized_pnl_eur": D(120), "costs_eur": D(2),
                       "net_pnl_eur": D(118), "trades_total": 1, "trades": [
                           {"portfolio": "Alfa", "symbol": "AAPL", "direction": "long",
                            "opened_at": datetime(2027, 3, 2, 15, tzinfo=UTC),
                            "closed_at": datetime(2027, 3, 5, 15, tzinfo=UTC),
                            "qty": D(10), "open_qty": D(0), "currency": "USD",
                            "entry_price": D(180), "exit_price": D(192),
                            "realized_pnl_eur": D(120), "costs_eur": D(2),
                            "net_pnl_eur": D(118)}]},
        "strategies": [StrategyResult(
            ref="STR-1", name="Momentum", status="attiva", portfolios=["Alfa"],
            trades_opened=2, trades_closed=1, wins=1, win_rate_pct=D("100.0"),
            realized_pnl_eur=D(120), costs_eur=D(2), net_pnl_eur=D(118),
            avg_holding_s=3600, open_positions=1)],
        "goals": [{"name": "campagna", "portfolio_name": "Alfa", "kind": "min",
                   "period_start": days[0], "period_end": days[-1], "target_pct": D(3),
                   "pct": D("2.5"), "status": "concluso", "benchmark": None,
                   "closes_positions": True}],
        "agent_costs": {"cost_usd": D("12.5"), "billed_usd": D("2.5"), "runs": 30,
                        "agents": [{"agent": "CEO", "cost_usd": D("12.5"),
                                    "billed_usd": D("2.5"), "runs": 30}]},
        "lessons": [{"created_at": datetime(2027, 3, 3, tzinfo=UTC), "kind": "lesson",
                     "agent": "Risk Manager", "summary": "Stop troppo stretto → uscita",
                     "status": None}],
        "team_rules": [{"created_at": datetime(2027, 3, 4, tzinfo=UTC), "kind": "team_rule",
                        "agent": "CEO", "summary": "Mai più del 10% su un titolo",
                        "status": "active"}],
        "decisions": [{"created_at": datetime(2027, 3, 4, tzinfo=UTC), "kind": "veto",
                       "agent": "Risk Manager", "portfolio": "Alfa",
                       "summary": "Veto su TSLA", "reason": "volatilità"}],
    }
    data.update(over)
    return data


def _text(raw: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(raw)).pages)


def test_pdf_has_the_italian_sections():
    raw = pdf.render_pdf(_data())
    assert raw.startswith(b"%PDF")
    text = _text(raw)
    for title in ("Resoconto campagna «Prova di marzo»", "Sintesi", "Andamento",
                  "Rendimento per portafoglio", "Operazioni", "Risultati per strategia",
                  "Obiettivi", "Costi degli agenti", "Lezioni e regole del team",
                  "Decisioni rilevanti"):
        assert title in text, title
    assert "10.250,50 EUR" in text and "+2,50%" in text  # Italian formats
    assert "Momentum" in text and "Veto su TSLA" in text and "Mai più del 10%" in text
    assert "Test con EUR e - segni" in text  # € and − replaced (Latin-1 fonts)
    assert "Pagina 1 di" in text


def test_pdf_without_any_data():
    empty = _data(campaign=None, title="Resoconto del periodo", portfolios=[],
                  total={"start_eur": None, "end_eur": None, "change_eur": None,
                         "return_pct": None, "max_drawdown_pct": None, "curve": []},
                  strategies=[], goals=[], lessons=[], team_rules=[], decisions=[],
                  benchmarks=[{"symbol": s, "label": s, "change_pct": None, "diff_pp": None,
                               "series": []} for s in ("SPY", "QQQ", "VWCE.DE")],
                  operations={"fills": 0, "fees_eur": D(0), "trades_opened": 0,
                              "trades_closed": 0, "realized_pnl_eur": D(0),
                              "costs_eur": D(0), "net_pnl_eur": D(0), "trades": [],
                              "trades_total": 0},
                  agent_costs={"cost_usd": D(0), "billed_usd": D(0), "runs": 0,
                               "agents": []})
    text = _text(pdf.render_pdf(empty))
    assert "Resoconto del periodo" in text
    assert "Dati insufficienti per il grafico" in text
    assert "Nessuna operazione nel periodo." in text
    assert "dati non disponibili" in text


def test_formats_and_filename():
    assert pdf.money(D("-1234.5")) == "-1.234,50 EUR"
    assert pdf.pct(D(0)) == "0,00%" and pdf.pct(None) == "n.d."
    assert pdf.filename(_data()) == "resoconto-prova-di-marzo-2027-03-01-2027-03-10.pdf"
    assert pdf.filename(_data(campaign=None)) == "resoconto-2027-03-01-2027-03-10.pdf"
