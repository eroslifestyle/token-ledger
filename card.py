#!/usr/bin/env python3
"""Card fluttuante — widget grafico Token Ledger (PySide6).

Finestra frameless semi-trasparente, trascinabile, sempre-in-primo-piano (hint).
Nota Wayland: GNOME-Mutter non permette il pin-allo-sfondo; resta finestra normale.
Grafici via QPainter custom (niente QtCharts -> piu' leggero).

Filtri (pulsanti in alto): PERIODO (oggi/7gg/30gg) e PROVIDER (Tutti/Claude/MiniMax/Locali).
Due lenti di costo:
  - "a consumo" = listino API (quanto costerebbe a token) -> notional, NON pagato
  - "reale"     = quanto paghi davvero: abbonamenti flat amortizzati + eventuale API a consumo

Lancia: tokenstats card   ·   click+trascina per spostare   ·   X o Esc per chiudere.
"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QPoint, QRectF, QProcess
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QBrush, QPainterPath
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolTip,
)

BASE = Path(__file__).resolve().parent
DB = BASE / "ledger.db"
SRC = "('claude_code','litellm')"
ACCENT = "#7aa2f7"
GREEN = "#9ece6a"
GOLD = "#e0af68"
MUTED = "#9aa5ce"
BG = QColor(24, 25, 36, 252)  # quasi opaco (alpha 252/255) — niente bleed-through dal desktop
REFRESH_MS = 10000
DAYS_PER_MONTH = 30.44
FREE_KEYS = ("qwen", "deepseek", "gemma", "llama", "ollama")  # locali su leobox -> ~0

# filtri provider: etichetta -> tuple di keyword sul nome modello (None = tutti)
PROVIDERS = [
    ("Tutti", None),
    ("Claude", ("opus", "sonnet", "haiku", "fable", "claude")),
    ("MiniMax", ("minimax", "abab")),
    ("Locali", FREE_KEYS),
]
PERIODS = [("oggi", 1), ("7gg", 7), ("30gg", 30), ("3mesi", 90), ("totale", 0)]


def human(n):
    n = float(n or 0)
    for u in ("", "K", "M", "B"):
        if abs(n) < 1000:
            return f"{n:.1f}{u}".replace(".0", "")
        n /= 1000
    return f"{n:.1f}T"


def load_subs():
    try:
        return json.loads((BASE / "prices.json").read_text()).get("subscriptions", [])
    except Exception:
        return []


def real_kind(model, subs):
    """'sub' = coperto da abbonamento flat; 'free' = locale ~0; 'paid' = pay-per-use reale."""
    m = (model or "").lower()
    for s in subs:
        if any(str(k).lower() in m for k in s.get("keys", [])):
            return "sub"
    if any(k in m for k in FREE_KEYS):
        return "free"
    return "paid"


def _model_clause(prov_keys):
    """Clausola SQL per filtrare i modelli del provider scelto (None = nessun filtro)."""
    if not prov_keys:
        return ""
    likes = " OR ".join("LOWER(model) LIKE '%" + k + "%'" for k in prov_keys)
    return f" AND ({likes})"


def fetch(days=1, prov_keys=None):
    if not DB.exists():
        return None
    c = sqlite3.connect(str(DB))
    now = int(time.time())
    since = 0 if days == 0 else now - days * 86400
    subs = load_subs()
    mclause = _model_clause(prov_keys)

    def one(sql, *a):
        return c.execute(sql, a).fetchone()

    tok1, consumo1 = one(
        f"SELECT SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens),"
        f"SUM(cost_usd) FROM usage_events WHERE source IN {SRC} AND ts>=?{mclause}", since)
    tok1 = tok1 or 0
    consumo1 = float(consumo1 or 0)

    rows = c.execute(
        f"SELECT model, "
        f"SUM(input_tokens+cache_read_tokens+cache_creation_tokens) intok, "
        f"SUM(output_tokens) outtok, SUM(cost_usd) co "
        f"FROM usage_events WHERE source IN {SRC} AND ts>=?{mclause} "
        f"GROUP BY model ORDER BY intok+outtok DESC LIMIT 8", (since,)).fetchall()
    models = []
    for m, itk, otk, co in rows:
        itk = itk or 0
        otk = otk or 0
        tk = itk + otk
        models.append({
            "model": m or "?", "in_tok": itk, "out_tok": otk, "tok": tk,
            "consumo": float(co or 0), "kind": real_kind(m, subs),
            "pct": 100 * tk / tok1 if tok1 else 0,
        })

    cr, inp = one("SELECT SUM(cache_read_tokens),SUM(input_tokens) FROM usage_events "
                  f"WHERE source='claude_code' AND ts>=?{mclause}", since)
    # sparkline coerente col periodo + provider: oggi -> 24 bucket orari; 3mesi -> 90; totale -> 120 bucket settimanali
    if days == 0:
        nb, bucket = 120, 7 * 86400  # ~2.3 anni settimanale
    elif days <= 1:
        nb, bucket = 24, 3600
    else:
        nb, bucket = days, 86400
    sp_start = max(since, now - nb * bucket)
    spark = c.execute(
        f"SELECT CAST((?-ts)/{bucket} AS INT) b, SUM(cost_usd) FROM usage_events "
        f"WHERE source IN {SRC} AND ts>=?{mclause} GROUP BY b", (now, sp_start)).fetchall()
    sav = one("SELECT savings_usd FROM savings_snapshot ORDER BY ts DESC LIMIT 1")
    c.close()

    spark_vals = [0.0] * nb
    for b, co in spark:
        if 0 <= b < nb:
            spark_vals[nb - 1 - b] = float(co or 0)
    fmt = "%H:00" if days <= 1 else "%d/%m"
    spark_labels = [time.strftime(fmt, time.localtime(now - (nb - 1 - j) * bucket)) for j in range(nb)]

    # costo reale: abbonamenti flat (solo quelli del provider filtrato) amortizzati * giorni + pay-per-use
    prov_set = set(k.lower() for k in prov_keys) if prov_keys else None

    def sub_matches(s):
        return prov_set is None or any(str(k).lower() in prov_set for k in s.get("keys", []))
    sub_break = [(s.get("name", "?"), float(s.get("monthly_usd", 0) or 0) / DAYS_PER_MONTH * days)
                 for s in subs if sub_matches(s)]
    paid_real = sum(mo["consumo"] for mo in models if mo["kind"] == "paid")
    real_total = sum(v for _, v in sub_break) + paid_real

    return {
        "tok1": tok1, "consumo1": consumo1, "models": models,
        "cache_pct": 100 * (cr or 0) / ((cr or 0) + (inp or 0)) if (cr or inp) else 0,
        "spark": spark_vals, "spark_labels": spark_labels, "savings": float(sav[0]) if sav else 0.0,
        "sub_break": sub_break, "paid_real": paid_real, "real_total": real_total,
    }


class Spark(QWidget):
    def __init__(self):
        super().__init__()
        self.vals = []
        self.labels = []
        self._pts = []
        self._hover = -1
        self.setMinimumHeight(44)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)  # hover senza bottone premuto

    def set_vals(self, vals, labels=None):
        self.vals = vals
        self.labels = labels or [""] * len(vals)
        self._hover = -1
        self.update()

    def paintEvent(self, _):
        if not self.vals:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        mx = max(self.vals) or 1
        n = len(self.vals)
        step = w / max(n - 1, 1)
        path = QPainterPath()
        self._pts = []
        for i, v in enumerate(self.vals):
            x = i * step
            y = h - 6 - (v / mx) * (h - 14)
            self._pts.append((x, y))
            path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
        fill = QPainterPath(path)
        fill.lineTo(w, h)
        fill.lineTo(0, h)
        fill.closeSubpath()
        p.fillPath(fill, QBrush(QColor(122, 162, 247, 45)))
        p.setPen(QPen(QColor(ACCENT), 2))
        p.drawPath(path)
        for i, (x, y) in enumerate(self._pts):
            if i == self._hover:
                p.setPen(QPen(QColor(255, 255, 255, 70), 1))
                p.drawLine(int(x), 0, int(x), h)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#ffffff"))
                p.drawEllipse(QPoint(int(x), int(y)), 4, 4)
            else:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(GREEN))
                p.drawEllipse(QPoint(int(x), int(y)), 2, 2)

    def mouseMoveEvent(self, e):
        if not self._pts:
            return
        mx = e.position().x()
        idx = min(range(len(self._pts)), key=lambda i: abs(self._pts[i][0] - mx))
        if abs(self._pts[idx][0] - mx) <= 14:
            if idx != self._hover:
                self._hover = idx
                self.update()
            lab = self.labels[idx] if idx < len(self.labels) else ""
            QToolTip.showText(e.globalPosition().toPoint(),
                              f"{lab}  ·  ${self.vals[idx]:,.2f} a consumo", self)
        else:
            self._clear_hover()

    def leaveEvent(self, _):
        self._clear_hover()

    def _clear_hover(self):
        if self._hover != -1:
            self._hover = -1
            self.update()
        QToolTip.hideText()


class ModelTable(QWidget):
    """Tabella per-modello: modello | input | output | consumo | reale.
    input = prompt (input freschi + cache read/write) · output = token generati."""
    HEADERS = {"mod": "modello", "in": "input", "out": "output", "cons": "consumo", "real": "reale"}

    def __init__(self):
        super().__init__()
        self.rows = []
        self.setMinimumHeight(150)

    def set_rows(self, rows):
        self.rows = rows
        self.update()

    @staticmethod
    def _cols(w):
        return [
            (0,       w - 218, Qt.AlignLeft | Qt.AlignVCenter,  "mod"),
            (w - 218, 52,      Qt.AlignRight | Qt.AlignVCenter, "in"),
            (w - 162, 48,      Qt.AlignRight | Qt.AlignVCenter, "out"),
            (w - 108, 64,      Qt.AlignRight | Qt.AlignVCenter, "cons"),
            (w - 42,  42,      Qt.AlignRight | Qt.AlignVCenter, "real"),
        ]

    def _real_text(self, mo):
        if mo["kind"] == "sub":
            return "abbon.", QColor(GREEN)
        if mo["kind"] == "paid":
            return f"${mo['consumo']:,.0f}", QColor(GOLD)
        return "$0", QColor("#6a72a0")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        cols = self._cols(w)
        rh = 19
        p.setFont(QFont("Sans", 8))
        p.setPen(QColor("#6a72a0"))
        for x, cw, al, key in cols:
            p.drawText(QRectF(x, 0, cw, rh), al, self.HEADERS[key])
        p.setPen(QPen(QColor("#2a2e42"), 1))
        p.drawLine(0, rh, w, rh)

        if not self.rows:
            p.setPen(QColor("#6a72a0"))
            p.drawText(QRectF(0, rh + 8, w, 20), Qt.AlignCenter, "(nessun dato per questo filtro)")
            return
        y = rh + 5
        for mo in self.rows:
            real_txt, real_col = self._real_text(mo)
            vals = {
                "mod": (mo["model"][:16], QColor("#c0caf5")),
                "in": (human(mo["in_tok"]), QColor(MUTED)),
                "out": (human(mo["out_tok"]), QColor("#7dcfff")),
                "cons": (f"${mo['consumo']:,.0f}", QColor(MUTED)),
                "real": (real_txt, real_col),
            }
            p.setFont(QFont("Sans", 9))
            for x, cw, al, key in cols:
                txt, col = vals[key]
                p.setPen(col)
                p.drawText(QRectF(x, y, cw, rh), al, txt)
            y += rh + 3


class Card(QWidget):
    PILL = ("QPushButton{{color:{fg};background:{bg};border:none;border-radius:9px;"
            "padding:2px 9px;font-size:10px;}}QPushButton:hover{{color:#fff;}}")

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setWindowTitle("Token Ledger")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(410, 560)
        self._drag = None
        self.days = 1
        self.prov_keys = None
        self._period_btns = {}
        self._prov_btns = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(4)

        # collector on-demand (async, non blocca la GUI)
        self._proc = QProcess(self)
        self._proc.finished.connect(self._collect_done)

        top = QHBoxLayout()
        title = QLabel("🪙 Token Ledger")
        title.setStyleSheet(f"color:{ACCENT};font-weight:bold;font-size:15px;")
        self.btn_refresh = QPushButton("↻")
        self.btn_refresh.setFixedSize(22, 22)
        self.btn_refresh.setToolTip("Aggiorna ora — lancia il collector (dati nel DB ogni 1min in automatico)")
        self.btn_refresh.clicked.connect(self._manual_collect)
        self.btn_refresh.setStyleSheet(
            "QPushButton{color:#7aa2f7;border:none;font-size:15px;}QPushButton:hover{color:#fff;}")
        x = QPushButton("✕")
        x.setFixedSize(22, 22)
        x.clicked.connect(self.close)
        x.setStyleSheet("QPushButton{color:#888;border:none;font-size:14px;}QPushButton:hover{color:#f77;}")
        top.addWidget(title)
        top.addStretch()
        top.addWidget(self.btn_refresh)
        top.addWidget(x)
        lay.addLayout(top)

        # filtri
        lay.addLayout(self._filter_row(PERIODS, self._period_btns, self._set_period))
        lay.addLayout(self._filter_row([(n, k) for n, k in PROVIDERS], self._prov_btns, self._set_prov))

        self.big = QLabel()
        self.big.setStyleSheet(f"color:{GREEN};font-size:25px;font-weight:bold;")
        lay.addWidget(self.big)
        self.sub = QLabel()
        self.sub.setStyleSheet("color:#c0caf5;font-size:12px;")
        lay.addWidget(self.sub)

        self.cap_models = self._cap("Per modello")
        lay.addWidget(self.cap_models)
        self.table = ModelTable()
        lay.addWidget(self.table)

        lay.addWidget(self._cap("Costo reale pagato"))
        self.real = QLabel()
        self.real.setStyleSheet(f"color:{GREEN};font-size:13px;font-weight:bold;")
        lay.addWidget(self.real)
        self.kpi = QLabel()
        self.kpi.setStyleSheet("color:#9aa5ce;font-size:11px;")
        self.kpi.setWordWrap(True)
        lay.addWidget(self.kpi)

        self.cap_spark = self._cap("Andamento costo a consumo")
        lay.addWidget(self.cap_spark)
        self.spark = Spark()
        lay.addWidget(self.spark)
        self.foot = QLabel()
        self.foot.setStyleSheet("color:#565f89;font-size:9px;")
        self.foot.setWordWrap(True)
        lay.addWidget(self.foot)

        self._sync_btns()
        self.refresh()
        t = QTimer(self)
        t.timeout.connect(self.refresh)
        t.start(REFRESH_MS)

    def _filter_row(self, items, store, cb):
        row = QHBoxLayout()
        row.setSpacing(5)
        for name, val in items:
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, v=val: cb(v))
            store[name] = (b, val)
            row.addWidget(b)
        row.addStretch()
        return row

    def _set_period(self, days):
        self.days = days
        self._sync_btns()
        self.refresh()

    def _set_prov(self, keys):
        self.prov_keys = keys
        self._sync_btns()
        self.refresh()

    def _sync_btns(self):
        for name, (b, val) in self._period_btns.items():
            on = val == self.days
            b.setStyleSheet(self.PILL.format(fg="#1a1b26" if on else MUTED, bg=ACCENT if on else "#24283b"))
        for name, (b, val) in self._prov_btns.items():
            on = val == self.prov_keys
            b.setStyleSheet(self.PILL.format(fg="#1a1b26" if on else MUTED, bg=GREEN if on else "#24283b"))

    def _cap(self, txt):
        l = QLabel(txt)
        l.setStyleSheet("color:#7a82a8;font-size:10px;margin-top:5px;")
        return l

    def _manual_collect(self):
        if self._proc.state() != QProcess.NotRunning:
            return  # già in corso
        self.btn_refresh.setText("…")
        self.btn_refresh.setEnabled(False)
        self._proc.start("python3", [str(BASE / "collect.py"), "--quick"])

    def _collect_done(self, *_):
        self.btn_refresh.setText("↻")
        self.btn_refresh.setEnabled(True)
        self.refresh()

    def refresh(self):
        d = fetch(self.days, self.prov_keys)
        plabel = {1: "oggi", 7: "ultimi 7gg", 30: "ultimi 30gg"}.get(self.days, f"{self.days}gg")
        self.cap_models.setText(f"Per modello · {plabel}")
        if not d:
            self.big.setText("—")
            self.sub.setText("Ledger vuoto")
            return
        self.big.setText(f"{human(d['tok1'])} token")
        self.sub.setText(
            f"{plabel} · reale <b>${d['real_total']:,.2f}</b> · a consumo (listino) ${d['consumo1']:,.0f}")
        self.table.set_rows(d["models"])

        self.real.setText(f"${d['real_total']:,.2f}  ({plabel})")
        parts = [f"{name.split()[0]} ${v:,.2f}" for name, v in d["sub_break"]]
        if d["paid_real"] > 0:
            parts.append(f"API ${d['paid_real']:,.2f}")
        bd = "  +  ".join(parts) if parts else "nessun abbonamento attivo nel filtro"
        self.kpi.setText(f"abbonamenti flat: {bd}   ·   cache hit {d['cache_pct']:.0f}%"
                         f"   ·   risparmio cache ${d['savings']:,.0f}")

        self.cap_spark.setText(
            f"Andamento costo a consumo · {'24h (orario)' if self.days == 1 else plabel + ' (giornaliero)'}")
        self.spark.set_vals(d["spark"], d.get("spark_labels"))
        self.foot.setText(
            "input = prompt (freschi + cache) · output = generati · "
            "a consumo = listino API (non pagato, sei flat) · reale = abbonamenti/30.44gg + API · "
            "agg. " + time.strftime("%H:%M:%S"))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(BG))
        p.setPen(QPen(QColor(ACCENT), 1))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 16, 16)

    def mousePressEvent(self, e):
        # Wayland ignora self.move(): il drag va chiesto al compositor con startSystemMove().
        # Funziona anche su X11. Fallback a move() manuale se non supportato.
        if e.button() == Qt.LeftButton:
            wh = self.windowHandle()
            if wh is not None and wh.startSystemMove():
                e.accept()
                return
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    app.setDesktopFileName("tokenstats")  # associa finestra <-> launcher (icona/app_id)
    app.setApplicationName("Token Stats")
    w = Card()
    w.show()
    scr = app.primaryScreen().availableGeometry()
    w.move(scr.center().x() - w.width() // 2, scr.center().y() - w.height() // 2)
    w.raise_()
    w.activateWindow()
    sys.exit(app.exec())
