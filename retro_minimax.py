#!/usr/bin/env python3
"""
Retro-correction script for MiniMax-M3 misattribution in ledger.db.
Conservative: exports pending events to CSV, requires external evidence to apply fixes.
"""

import sqlite3
import json
import sys
from pathlib import Path

def main():
    db_path = Path.home() / ".claude" / "token-ledger" / "ledger.db"
    csv_path = Path.home() / ".claude" / "token-ledger" / "retro_minimax_pending.csv"

    if not db_path.exists():
        print(f"Error: {db_path} not found")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Count rows with MiniMax-M3
    cur.execute("SELECT COUNT(*) as cnt FROM usage_events WHERE model='MiniMax-M3'")
    count = cur.fetchone()["cnt"]

    print(f"\n--- Retro-correction Summary ---")
    print(f"Found {count} events with model='MiniMax-M3' in ledger.db")

    if count == 0:
        print("No events to process. Exiting.")
        conn.close()
        sys.exit(0)

    print(f"\nWARNING: Historical model attribution cannot be safely inferred.")
    print(f"This script will export affected events to CSV for manual review.\n")

    # Ask for confirmation
    resp = input(f"Export {count} events to retro_minimax_pending.csv? (yes/no): ").strip().lower()
    if resp != "yes":
        print("Cancelled.")
        conn.close()
        sys.exit(0)

    # Export to CSV
    cur.execute("""
        SELECT event_uuid, ts, source, session_id, model
        FROM usage_events
        WHERE model='MiniMax-M3'
        ORDER BY ts DESC
    """)
    rows = cur.fetchall()

    with open(csv_path, "w") as f:
        f.write("event_uuid,ts,source,session_id,model_originale,ipotesi\n")
        for row in rows:
            ts_iso = Path(row["ts"]).name if isinstance(row["ts"], str) else row["ts"]
            f.write(f'{row["event_uuid"]},{ts_iso},{row["source"]},{row["session_id"]},MiniMax-M3,unknown\n')

    print(f"\n✓ Exported {count} events to {csv_path}")
    print("\nNext step: Provide external evidence (session_id → actual model mapping)")
    print("Then re-run with: python3 retro_minimax.py --apply <mapping.json>")

    conn.close()

if __name__ == "__main__":
    main()
