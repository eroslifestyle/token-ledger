#!/usr/bin/env python3
"""Seed ledger.db with 30 synthetic events so you can try the widget without real data.

Run once: python3 seed_demo.py
Safe to re-run: drops the demo events first (those with source='demo_seed').
"""
from __future__ import annotations
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "ledger.db"
PRICES_PATH = BASE / "prices.json"

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
CREATE INDEX IF NOT EXISTS idx_ts     ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_model  ON usage_events(model);
CREATE INDEX IF NOT EXISTS idx_source ON usage_events(source);
"""


def price_for(model: str, prices: dict, order: list[str]) -> dict:
    m = (model or "").lower()
    for key in order:
        if key in m:
            return prices.get(key, prices["default"])
    return prices["default"]


def cost(model, inp, out, cread, cwrite, prices, order) -> float:
    p = price_for(model, prices, order)
    return (inp * p["input"] + out * p["output"]
            + cread * p["cache_read"] + cwrite * p["cache_write"]) / 1_000_000.0


def main() -> int:
    if not DB.exists():
        sys.exit(f"ledger.db not found at {DB}. Run collect.py once first to bootstrap schema.")

    prices_doc = json.loads(PRICES_PATH.read_text())
    order = prices_doc["_order"]
    prices = prices_doc["prices"]

    con = sqlite3.connect(str(DB))
    con.executescript(SCHEMA)
    con.execute("DELETE FROM usage_events WHERE source='demo_seed'")
    con.commit()

    now = int(time.time())
    seven_days = 7 * 86400

    # 30 synthetic events spread across the last 7 days, mixed providers
    samples = [
        # (model, source, project, hours_ago, in, out, cread, cwrite, session_id)
        ("claude-opus-4-8",                   "claude_code", ".claude",         2,   4521,  812, 3200, 400, "demo-1"),
        ("claude-opus-4-8",                   "claude_code", ".claude",         5,   8920, 1340, 7100, 600, "demo-1"),
        ("claude-sonnet-4-6",                 "claude_code", "ai-router-switch", 1,  3210,  445, 2800, 200, "demo-2"),
        ("claude-sonnet-4-6",                 "claude_code", "ai-router-switch", 3,  5640,  712, 5100, 350, "demo-2"),
        ("claude-sonnet-4-6",                 "claude_code", "token-ledger",     6,  1820,  201, 1500, 100, "demo-3"),
        ("claude-haiku-4-5-20251001",         "claude_code", ".claude",         8,    980,  134,   800,  50, "demo-1"),
        ("claude-haiku-4-5-20251001",         "claude_code", ".claude",        12,   1450,  198,  1200,  80, "demo-1"),
        ("MiniMax-M3",                        "claude_code", "ai-router-switch", 4, 6210,  910,    0,   0, "demo-2"),
        ("MiniMax-M3",                        "claude_code", "ai-router-switch", 9, 4320,  602,    0,   0, "demo-2"),
        ("MiniMax-M3",                        "claude_code", "token-ledger",    15, 2810,  410,    0,   0, "demo-3"),
        ("MiniMax-M2.7-highspeed",            "litellm",     "ai-router-switch", 7,  720,  180,    0,   0, "demo-4"),
        ("MiniMax-M2.7-highspeed",            "litellm",     "ai-router-switch",18,  410,   95,    0,   0, "demo-4"),
        ("MiniMax-M2.5-highspeed",            "litellm",     "Manus",           22,  290,   62,    0,   0, "demo-5"),
        ("openai/MiniMax-M3",                 "litellm",     "ai-router-switch", 2, 9120, 1520,    0,   0, "demo-6"),
        ("openai/MiniMax-M3",                 "litellm",     "ai-router-switch",11, 6830, 1102,    0,   0, "demo-6"),
        ("openai/MiniMax-M3",                 "litellm",     "token-ledger",    17, 4120,  680,    0,   0, "demo-7"),
        ("ollama_chat/qwen3.6-opus-abliterated:35b", "litellm", "Debinex",     3, 1820,  340,    0,   0, "demo-8"),
        ("ollama_chat/qwen3.6-opus-abliterated:35b", "litellm", "Debinex",     14, 2210,  410,    0,   0, "demo-8"),
        ("ollama_chat/qwen3-coder-abliterated:30b","litellm", "ai-router-switch", 5,  980, 220,    0,   0, "demo-9"),
        ("ollama_chat/chat-max",              "litellm",     ".claude",         8,   210,   45,    0,   0, "demo-10"),
        ("ollama_chat/code-max",              "litellm",     "token-ledger",   19,   340,   78,    0,   0, "demo-11"),
        ("claude-fable-5",                    "claude_code", ".claude",        21,  1820,  290, 1500, 100, "demo-12"),
        ("claude-fable-5",                    "claude_code", ".claude",        26,  1410,  221, 1100,  80, "demo-12"),
        ("groq/llama-3.3-70b-versatile",      "litellm",     "ai-router-switch", 6,  680,  142,    0,   0, "demo-13"),
        ("cerebras/gpt-oss-120b",             "litellm",     "Debinex",        13,  420,   91,    0,   0, "demo-14"),
        ("ollama_chat/fast-max",              "litellm",     ".claude",        16,   180,   36,    0,   0, "demo-15"),
        ("claude-opus-4-8",                   "claude_code", "Manus",          30,  3210,  488, 2500, 200, "demo-16"),
        ("claude-sonnet-4-6",                 "claude_code", "ai-router-switch",24, 2340,  321, 2000, 150, "demo-2"),
        ("openai/MiniMax-M3",                 "litellm",     "ai-router-switch",33, 5210,  832,    0,   0, "demo-6"),
        ("MiniMax-M2.7-highspeed",            "litellm",     "token-ledger",   42,  340,   85,    0,   0, "demo-4"),
    ]

    inserted = 0
    for model, source, project, hours_ago, inp, out, cread, cwrite, sid in samples:
        ev = {
            "event_uuid": str(uuid.uuid4()),
            "ts": now - hours_ago * 3600,
            "source": "demo_seed",
            "session_id": sid,
            "project": project,
            "model": model,
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cread,
            "cache_creation_tokens": cwrite,
            "cost_usd": cost(model, inp, out, cread, cwrite, prices, order),
        }
        try:
            con.execute(
                "INSERT INTO usage_events(event_uuid,ts,source,session_id,project,model,"
                "input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ev["event_uuid"], ev["ts"], ev["source"], ev["session_id"], ev["project"],
                 ev["model"], ev["input_tokens"], ev["output_tokens"],
                 ev["cache_read_tokens"], ev["cache_creation_tokens"], ev["cost_usd"]),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            pass

    con.commit()
    con.close()
    print(f"Seeded {inserted} demo events into {DB}")
    print(f"Run: python3 card.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())