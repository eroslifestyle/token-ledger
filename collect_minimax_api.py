#!/usr/bin/env python3
"""collect_minimax_api.py — pull stats ufficiali MiniMax via Subscription Key.

Usa la Subscription Key salvata in ~/.secrets/minimax.env
(MINIMAX_API_KEY, prefisso "sk-cp-...") per interrogare l'endpoint ufficiale
coding_plan/remains. Non serve login browser: la key è già un Bearer token.

Restituisce: per ogni model_name in model_remains:
  - current_interval_remaining_percent  (finestra 5h)
  - current_weekly_remaining_percent    (finestra settimanale)
  - current_interval_total_count / current_interval_usage_count (5h)
  - current_weekly_total_count   / current_weekly_usage_count  (sett.)

Non scrive nel DB del ledger direttamente — produce un dict che il widget
legge via widget_state.json (campo 'minimax_plan').
"""
from __future__ import annotations
import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path

ENDPOINT = "https://api.minimaxi.chat/v1/api/openplatform/coding_plan/remains"
SECRET_FILE = Path.home() / ".secrets" / "minimax.env"
TIMEOUT = 10


def _load_key() -> str | None:
    """Legge MINIMAX_API_KEY da .env o env shell (la Subscription Key)."""
    env_key = os.environ.get("MINIMAX_API_KEY")
    if env_key:
        return env_key.strip().strip('"')
    if not SECRET_FILE.exists():
        return None
    for line in SECRET_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        k, v = line.split("=", 1)
        if k.strip() == "MINIMAX_API_KEY":
            return v.strip().strip('"')
    return None


def fetch_plan() -> dict | None:
    """Chiama l'endpoint MiniMax e ritorna un dict normalizzato o None."""
    key = _load_key()
    if not key:
        return None
    req = urllib.request.Request(ENDPOINT, headers={
        "Authorization": f"Bearer {key}",
        "User-Agent": "token-ledger/1.0",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "ts": int(time.time())}
    except Exception as e:
        return {"error": str(e), "ts": int(time.time())}

    try:
        data = json.loads(raw)
    except Exception:
        return {"error": "json parse fail", "raw": raw[:200], "ts": int(time.time())}

    base = data.get("base_resp", {})
    if base.get("status_code") not in (0, None):
        return {"error": base.get("status_msg", "unknown api error"),
                "status_code": base.get("status_code"), "ts": int(time.time())}

    # Normalizza per ogni model_remains
    models = {}
    for entry in (data.get("model_remains") or []):
        name = entry.get("model_name", "unknown")
        models[name] = {
            "interval_5h": {
                "remaining_pct": entry.get("current_interval_remaining_percent"),
                "total_count": entry.get("current_interval_total_count"),
                "usage_count": entry.get("current_interval_usage_count"),
                "status": entry.get("current_interval_status"),
                "reset_at": entry.get("end_time"),
            },
            "weekly": {
                "remaining_pct": entry.get("current_weekly_remaining_percent"),
                "total_count": entry.get("current_weekly_total_count"),
                "usage_count": entry.get("current_weekly_usage_count"),
                "status": entry.get("current_weekly_status"),
                "reset_at": entry.get("weekly_end_time"),
            },
        }

    return {
        "ok": True,
        "models": models,
        "primary_model": "general",  # per il widget: focus su linguaggio
        "ts": int(time.time()),
    }


def main() -> int:
    result = fetch_plan()
    if result is None:
        print("Nessuna MINIMAX_API_KEY trovata in", SECRET_FILE)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if "error" not in result else 2


if __name__ == "__main__":
    import sys
    sys.exit(main())