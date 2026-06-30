#!/usr/bin/env python3
"""TUI live — "htop dei token". Textual. Si auto-aggiorna e rilancia il collector --quick.

Lancia: tokenstats live   (oppure  python3 live.py)   ·  q per uscire.
"""
from __future__ import annotations
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static, Header, Footer

BASE = Path(__file__).resolve().parent
DB = BASE / "ledger.db"
SRC = "('claude_code','litellm')"
REFRESH_S = 5


def human(n):
    n = float(n or 0)
    for u in ("", "K", "M", "B"):
        if abs(n) < 1000:
            return f"{n:.1f}{u}".replace(".0", "")
        n /= 1000
    return f"{n:.1f}T"


def q(c, sql, *a):
    return c.execute(sql, a).fetchall()


class Ledger(App):
    CSS = """
    Screen { background: $surface; }
    #grid { height: 1fr; }
    .panel { border: round $primary; padding: 1 2; margin: 1; width: 1fr; }
    #head { height: 5; content-align: center middle; color: $accent; text-style: bold; }
    .big { text-style: bold; color: $success; }
    """
    BINDINGS = [("q", "quit", "Esci"), ("r", "refresh", "Aggiorna")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="head")
        with Horizontal(id="grid"):
            with Vertical():
                yield Static(id="totals", classes="panel")
                yield Static(id="advise", classes="panel")
            yield Static(id="models", classes="panel")
            yield Static(id="projects", classes="panel")
        yield Footer()

    def on_mount(self):
        self.refresh_data()
        self.set_interval(REFRESH_S, self.refresh_data)

    def action_refresh(self):
        self.refresh_data()

    def refresh_data(self):
        # collector quick in background (non blocca la UI)
        try:
            subprocess.Popen([sys.executable, str(BASE / "collect.py"), "--quick"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        if not DB.exists():
            self.query_one("#head", Static).update("Ledger vuoto — lancia collect.py")
            return
        c = sqlite3.connect(str(DB))
        now = int(time.time())
        d1, d30 = now - 86400, now - 30 * 86400

        t = q(c, f"SELECT SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens),"
                 f"SUM(cost_usd),COUNT(*) FROM usage_events WHERE source IN {SRC} AND ts>=?", d1)[0]
        tok1, cost1, ev1 = (t[0] or 0), (t[1] or 0), (t[2] or 0)
        t30 = q(c, f"SELECT SUM(cost_usd) FROM usage_events WHERE source IN {SRC} AND ts>=?", d30)[0][0] or 0
        opus30 = q(c, "SELECT SUM(cost_usd) FROM usage_events WHERE LOWER(model) LIKE '%opus%' AND ts>=?", d30)[0][0] or 0
        cr, inp = q(c, "SELECT SUM(cache_read_tokens),SUM(input_tokens) FROM usage_events WHERE source='claude_code' AND ts>=?", d30)[0]
        hit = 100 * (cr or 0) / ((cr or 0) + (inp or 0)) if (cr or inp) else 0

        try:
            ws = json.loads((BASE / "widget_state.json").read_text())
        except Exception:
            ws = {}
        real1 = ws.get("today_real_cost_usd", 0)

        self.query_one("#head", Static).update("🪙  TOKEN LEDGER — live  (q=esci  r=aggiorna)")
        self.query_one("#totals", Static).update(
            f"[b]OGGI (24h)[/b]\n\n[green b]{human(tok1)}[/] token\n"
            f"${cost1:,.2f} a consumo (listino)\n[green]${real1:,.2f} reale pagato[/]\n{ev1:,} messaggi\n\n"
            f"[b]30 giorni[/b]\n${t30:,.2f} a consumo")
        self.query_one("#advise", Static).update(
            f"[b]Dove va il consumo[/b]\n\nOpus: [b]{100*opus30/t30 if t30 else 0:.1f}%[/] del costo\n"
            f"Cache hit: [b]{hit:.1f}%[/]\n\n[dim](costo = list-price equiv,\nsei su abbonamento)[/]")

        models = q(c, f"SELECT model,SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens) tk,"
                     f"SUM(cost_usd) co FROM usage_events WHERE source IN {SRC} AND ts>=? "
                     f"GROUP BY model ORDER BY co DESC LIMIT 8", d30)
        ml = "[b]Top modelli (30gg)[/b]\n\n" + "\n".join(
            f"{(m or '?')[:20]:20} {human(tk):>7} ${co:>9,.0f}" for m, tk, co in models)
        self.query_one("#models", Static).update(ml)

        projs = q(c, f"SELECT project,SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens) tk,"
                    f"SUM(cost_usd) co FROM usage_events WHERE source IN {SRC} AND ts>=? "
                    f"GROUP BY project ORDER BY co DESC LIMIT 8", d30)
        pl = "[b]Top progetti (30gg)[/b]\n\n" + "\n".join(
            f"{(p or '?')[:18]:18} {human(tk):>7} ${co:>9,.0f}" for p, tk, co in projs)
        self.query_one("#projects", Static).update(pl)
        c.close()


if __name__ == "__main__":
    Ledger().run()
