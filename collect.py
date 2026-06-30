#!/usr/bin/env python3
"""Token Ledger collector — raccoglie token da 2 fonti gia' esistenti in un unico SQLite.

Fonti (disaccoppiate, ognuna resiliente: se una fallisce le altre continuano):
  - claude_code : ~/.claude/projects/*/*.jsonl   (autoritativo per il consumo Anthropic di Claude Code)
  - litellm     : container Postgres litellm-db, tabella LiteLLM_SpendLogs (autoritativo per traffico :4000 = MiniMax + LLM locali)

Idempotente: dedup su PRIMARY KEY event_uuid + offset/cursori in collector_state. Rilanciarlo non duplica nulla.

Uso:
  python3 collect.py            # raccolta incrementale completa
  python3 collect.py --quick    # solo claude_code (veloce, per hook a fine sessione)
  python3 collect.py --selftest # test autocontenuti (non tocca il DB reale)
"""
from __future__ import annotations
import json
import os
import sqlite3
import subprocess
import sys
import time
import hashlib
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "ledger.db"
PRICES_PATH = BASE / "prices.json"
WIDGET_STATE = BASE / "widget_state.json"
ROUTER_SIDECAR = Path.home() / ".claude" / "logs" / "router-model-map.jsonl"

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Cache per router remap, TTL 60s
_remap_cache = {"time": 0, "data": {}}


_MM_NORM = {
    "minimax-m3": "MiniMax-M3",
    "minimax/minimax-m3": "MiniMax-M3",
    "openai/minimax-m3": "MiniMax-M3",
    "minimax-m2.7-hs": "MiniMax-M2.7-highspeed",
    "minimax-m2.7-highspeed": "MiniMax-M2.7-highspeed",
    "openai/minimax-m2.7-highspeed": "MiniMax-M2.7-highspeed",
    "minimax-m2.7": "MiniMax-M2.7",
    "openai/minimax-m2.7": "MiniMax-M2.7",
    "minimax-m2.5-hs": "MiniMax-M2.5-highspeed",
    "minimax-m2.5-highspeed": "MiniMax-M2.5-highspeed",
    "openai/minimax-m2.5-highspeed": "MiniMax-M2.5-highspeed",
    "minimax-m2.5": "MiniMax-M2.5",
    "openai/minimax-m2.5": "MiniMax-M2.5",
    "minimax-m2.1-hs": "MiniMax-M2.1-highspeed",
    "minimax-m2.1-highspeed": "MiniMax-M2.1-highspeed",
    "openai/minimax-m2.1-highspeed": "MiniMax-M2.1-highspeed",
    "minimax-m2.1": "MiniMax-M2.1",
    "openai/minimax-m2.1": "MiniMax-M2.1",
    "minimax-m2": "MiniMax-M2",
    "openai/minimax-m2": "MiniMax-M2",
}


def normalize_model(name: str) -> str:
    """Canonicalizza nomi modello: strip prefix openai/minimax/, lowercase variant → ProperCase."""
    if not name or name == "?":
        return name
    canon = _MM_NORM.get(name.lower())
    return canon if canon else name


def load_router_remap() -> dict:
    """Legge router-model-map.jsonl e ritorna dict {final: orig} per rimappatura.

    Aggrega tutte le righe contando (final, orig) e prende l'orig piu' frequente per ogni final.
    Cache con TTL 60s.
    """
    global _remap_cache
    now = time.time()

    # TTL cache 60s
    if now - _remap_cache["time"] < 60 and _remap_cache["data"]:
        return _remap_cache["data"]

    result = {}
    if not ROUTER_SIDECAR.exists():
        _remap_cache["time"] = now
        _remap_cache["data"] = result
        return result

    # Conteggio (final, orig)
    counts = {}
    try:
        for line in ROUTER_SIDECAR.read_text().splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                final = obj.get("final")
                orig = obj.get("orig")
                if final and orig:
                    key = (final, orig)
                    counts[key] = counts.get(key, 0) + 1
            except Exception:
                pass
    except Exception:
        pass

    # Aggrega per final, prendi orig piu' frequente
    for (final, orig), count in counts.items():
        if final not in result or count > counts.get((final, result[final]), 0):
            result[final] = orig

    _remap_cache["time"] = now
    _remap_cache["data"] = result
    return result
LITELLM_DB_CONTAINER = "litellm-db"
LITELLM_DB_USER = "litellm"
LITELLM_DB_NAME = "litellm"

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events(
  event_uuid            TEXT PRIMARY KEY,
  ts                    INTEGER NOT NULL,
  source                TEXT NOT NULL,
  session_id            TEXT,
  project               TEXT,
  model                 TEXT,
  input_tokens          INTEGER DEFAULT 0,
  output_tokens         INTEGER DEFAULT 0,
  cache_read_tokens     INTEGER DEFAULT 0,
  cache_creation_tokens INTEGER DEFAULT 0,
  cost_usd              REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS collector_state(
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS savings_snapshot(
  ts                INTEGER PRIMARY KEY,
  requests          INTEGER,
  tokens_saved      INTEGER,
  savings_usd       REAL,
  total_input_tokens INTEGER,
  total_input_usd   REAL
);
CREATE INDEX IF NOT EXISTS idx_ts     ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_model  ON usage_events(model);
CREATE INDEX IF NOT EXISTS idx_source ON usage_events(source);
CREATE INDEX IF NOT EXISTS idx_proj   ON usage_events(project);
"""


def load_prices():
    try:
        data = json.loads(PRICES_PATH.read_text())
        return data["_order"], data["prices"]
    except Exception:
        return ["default"], {"default": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}}


def price_for(model: str, order, prices):
    m = (model or "").lower()
    for key in order:
        if key in m:
            return prices.get(key, prices["default"])
    return prices["default"]


def cost_anthropic(model, inp, out, cread, cwrite, order, prices) -> float:
    p = price_for(model, order, prices)
    return (
        inp * p["input"] + out * p["output"]
        + cread * p["cache_read"] + cwrite * p["cache_write"]
    ) / 1_000_000.0


def db_connect(path=DB_PATH):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    return con


def get_state(con, key, default=None):
    row = con.execute("SELECT value FROM collector_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(con, key, value):
    con.execute(
        "INSERT INTO collector_state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def project_from_cwd(cwd: str) -> str:
    if not cwd:
        return "?"
    return os.path.basename(cwd.rstrip("/")) or cwd


# ---------------------------------------------------------------- claude_code
def parse_claude_line(line: str):
    """Estrae un evento da una riga JSONL Claude Code. Ritorna dict o None.

    Robusto: salta righe non-assistant, senza usage, o senza uuid.
    """
    try:
        d = json.loads(line)
    except Exception:
        return None
    if d.get("type") != "assistant":
        return None
    uuid = d.get("uuid")
    if not uuid:
        return None
    msg = d.get("message") or {}
    usage = msg.get("usage")
    if not usage:
        return None
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cread = int(usage.get("cache_read_input_tokens", 0) or 0)
    cwrite = int(usage.get("cache_creation_input_tokens", 0) or 0)
    if inp == 0 and out == 0 and cread == 0 and cwrite == 0:
        return None
    ts_raw = d.get("timestamp")
    ts = _parse_iso(ts_raw)
    model = msg.get("model") or "?"
    model = normalize_model(model)
    # Rimappatura MiniMax -> modello reale richiesto (via router sidecar)
    if model.startswith("MiniMax-"):
        remap = load_router_remap()
        if model in remap and remap[model]:
            model = remap[model]
    return {
        "event_uuid": uuid,
        "ts": ts,
        "source": "claude_code",
        "session_id": d.get("sessionId"),
        "project": project_from_cwd(d.get("cwd", "")),
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cread,
        "cache_creation_tokens": cwrite,
    }


def _parse_iso(ts_raw) -> int:
    if not ts_raw:
        return int(time.time())
    try:
        from datetime import datetime, timezone
        s = str(ts_raw).replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()) \
            if "+" not in s and "T" in s else int(datetime.fromisoformat(s).timestamp())
    except Exception:
        try:
            return int(float(ts_raw))
        except Exception:
            return int(time.time())


def collect_claude(con, order, prices) -> int:
    n = 0
    if not CLAUDE_PROJECTS.exists():
        return 0
    for jf in CLAUDE_PROJECTS.glob("*/*.jsonl"):
        key = f"claude:{jf}"
        try:
            size = jf.stat().st_size
        except OSError:
            continue
        last_off = int(get_state(con, key, "0"))
        if last_off > size:  # file ruotato/troncato -> riparti
            last_off = 0
        if last_off == size:
            continue
        try:
            with jf.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(last_off)
                for line in fh:
                    ev = parse_claude_line(line)
                    if ev is None:
                        continue
                    ev["cost_usd"] = cost_anthropic(
                        ev["model"], ev["input_tokens"], ev["output_tokens"],
                        ev["cache_read_tokens"], ev["cache_creation_tokens"], order, prices,
                    )
                    if _insert(con, ev):
                        n += 1
                new_off = fh.tell()
            set_state(con, key, new_off)
        except OSError:
            continue
    return n


# ---------------------------------------------------------------- litellm
def collect_litellm(con) -> int:
    """Legge LiteLLM_SpendLogs via `docker exec`. spend e' gia' il costo USD."""
    last = get_state(con, "litellm:last_start", "1970-01-01 00:00:00")
    q = (
        "SELECT request_id, EXTRACT(EPOCH FROM \"startTime\")::bigint, model, "
        "COALESCE(prompt_tokens,0), COALESCE(completion_tokens,0), COALESCE(spend,0), "
        "COALESCE(session_id,''), COALESCE(custom_llm_provider,''), \"startTime\" "
        "FROM \"LiteLLM_SpendLogs\" WHERE \"startTime\" > '%s' ORDER BY \"startTime\" ASC LIMIT 50000;"
        % last
    )
    cmd = [
        "docker", "exec", LITELLM_DB_CONTAINER, "psql", "-U", LITELLM_DB_USER,
        "-d", LITELLM_DB_NAME, "-tAF", "\t", "-c", q,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"[litellm] skip: {e}", file=sys.stderr)
        return 0
    if out.returncode != 0:
        print(f"[litellm] skip (psql rc={out.returncode}): {out.stderr.strip()[:160]}", file=sys.stderr)
        return 0
    n = 0
    max_start = last
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        rid, epoch, model, ptok, ctok, spend, sess, prov, raw_start = parts[:9]
        try:
            ev = {
                "event_uuid": f"litellm:{rid}",
                "ts": int(float(epoch)) if epoch else int(time.time()),
                "source": "litellm",
                "session_id": sess or None,
                "project": prov or "litellm",
                "model": normalize_model(model or "?"),
                "input_tokens": int(ptok or 0),
                "output_tokens": int(ctok or 0),
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "cost_usd": float(spend or 0),
            }
        except ValueError:
            continue
        if _insert(con, ev):
            n += 1
        if raw_start > max_start:
            max_start = raw_start
    set_state(con, "litellm:last_start", max_start)
    return n


def _insert(con, ev) -> bool:
    cur = con.execute(
        "INSERT OR IGNORE INTO usage_events"
        "(event_uuid,ts,source,session_id,project,model,input_tokens,output_tokens,"
        "cache_read_tokens,cache_creation_tokens,cost_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            ev["event_uuid"], ev["ts"], ev["source"], ev.get("session_id"),
            ev.get("project"), ev.get("model"), ev["input_tokens"], ev["output_tokens"],
            ev["cache_read_tokens"], ev["cache_creation_tokens"], ev["cost_usd"],
        ),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------- widget snapshot
def write_widget_state(con):
    day0 = int(time.time()) - 86400
    today_sql = (
        "SELECT COALESCE(SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens),0), "
        "COALESCE(SUM(cost_usd),0) FROM usage_events WHERE ts>=? AND source IN ('claude_code','litellm')"
    )
    tot_tok, tot_cost = con.execute(today_sql, (day0,)).fetchone()
    by_model = con.execute(
        "SELECT model, SUM(input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens) t, SUM(cost_usd) c "
        "FROM usage_events WHERE ts>=? AND source IN ('claude_code','litellm') "
        "GROUP BY model ORDER BY t DESC LIMIT 5", (day0,)
    ).fetchall()
    spark = con.execute(
        "SELECT CAST((? - ts)/86400 AS INT) d, SUM(cost_usd) "
        "FROM usage_events WHERE ts>=? AND source IN ('claude_code','litellm') GROUP BY d ORDER BY d DESC",
        (int(time.time()), int(time.time()) - 7 * 86400)
    ).fetchall()
    sav = con.execute("SELECT savings_usd, tokens_saved FROM savings_snapshot ORDER BY ts DESC LIMIT 1").fetchone()
    # costo REALE oggi: abbonamento Claude flat (amortizzato /30.44) + spend reale API a consumo
    # (MiniMax/abab pay-per-use); modelli locali ~0. tot_cost = "a consumo" (listino notional).
    try:
        subs = json.loads(PRICES_PATH.read_text()).get("subscriptions", [])
    except Exception:
        subs = []
    sub_day = sum(float(s.get("monthly_usd", 0) or 0) for s in subs) / 30.44
    _FREE_K = ("qwen", "deepseek", "gemma", "llama", "ollama")

    def _kind(m):
        ml = (m or "").lower()
        for s in subs:
            if any(str(k).lower() in ml for k in s.get("keys", [])):
                return "sub"
        if any(k in ml for k in _FREE_K):
            return "free"
        return "paid"
    paid_rows = con.execute(
        "SELECT model, source, SUM(cost_usd) FROM usage_events "
        "WHERE ts>=? AND source IN ('claude_code','litellm') GROUP BY model, source", (day0,)).fetchall()
    paid_real = sum(float(c or 0) for m, s, c in paid_rows if _kind(m) == "paid")
    real_today = sub_day + paid_real
    state = {
        "updated": int(time.time()),
        "today_tokens": int(tot_tok),
        "today_cost_usd": round(float(tot_cost), 4),
        "today_consumo_cost_usd": round(float(tot_cost), 4),
        "today_real_cost_usd": round(real_today, 2),
        "sub_day_usd": round(sub_day, 2),
        "paid_real_usd": round(paid_real, 2),
        "by_model": [{"model": m, "tokens": int(t), "cost_usd": round(float(c), 4)} for m, t, c in by_model],
        "spark_cost_7d": [round(float(c), 4) for _, c in sorted(spark)],
        "savings_usd": round(float(sav[0]), 2) if sav else 0.0,
        "tokens_saved": int(sav[1]) if sav else 0,
    }
    WIDGET_STATE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------- main
def run(quick=False):
    order, prices = load_prices()
    con = db_connect()
    n_claude = collect_claude(con, order, prices)
    n_lite = 0
    if not quick:
        n_lite = collect_litellm(con)
    con.commit()
    write_widget_state(con)
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    con.close()
    print(f"collected: claude_code +{n_claude}, litellm +{n_lite} | total events: {total}")


def selftest():
    import tempfile
    fails = []

    # 1. parsing riga claude valida
    sample = json.dumps({
        "type": "assistant", "uuid": "u-1", "timestamp": "2026-06-26T10:00:00.000Z",
        "sessionId": "s-1", "cwd": "/home/user/.claude",
        "message": {"model": "claude-opus-4-8", "usage": {
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 200, "cache_creation_input_tokens": 10}},
    })
    ev = parse_claude_line(sample)
    assert ev and ev["input_tokens"] == 100 and ev["output_tokens"] == 50, "parse usage"
    assert ev["cache_read_tokens"] == 200 and ev["project"] == ".claude", "parse cache/project"

    # 2. righe da scartare
    assert parse_claude_line('{"type":"user","uuid":"x"}') is None, "skip user"
    assert parse_claude_line("non-json") is None, "skip non-json"
    assert parse_claude_line(json.dumps({"type": "assistant", "uuid": "u2", "message": {}})) is None, "skip no-usage"

    # 3. pricing opus
    order, prices = load_prices()
    c = cost_anthropic("claude-opus-4-8", 1_000_000, 0, 0, 0, order, prices)
    assert abs(c - 15.0) < 1e-6, f"opus input price got {c}"
    c2 = cost_anthropic("claude-sonnet-4-6", 0, 1_000_000, 0, 0, order, prices)
    assert abs(c2 - 15.0) < 1e-6, f"sonnet output price got {c2}"

    # 4. idempotenza dedup su DB temporaneo
    with tempfile.TemporaryDirectory() as td:
        dbp = Path(td) / "t.db"
        con = db_connect(dbp)
        ev["cost_usd"] = 1.23
        assert _insert(con, ev) is True, "first insert"
        assert _insert(con, ev) is False, "dedup second insert"
        con.commit()
        cnt = con.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        assert cnt == 1, f"expected 1 row got {cnt}"
        con.close()

    print("SELFTEST OK (parsing + skip + pricing + dedup idempotente)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    run(quick="--quick" in sys.argv)
