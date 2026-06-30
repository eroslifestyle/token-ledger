# Token Ledger

A self-hosted token-usage dashboard that aggregates spending across multiple LLM providers
(Anthropic Claude, MiniMax, local Ollama models, and any OpenAI-compatible endpoint that
routes through [LiteLLM](https://github.com/BerriAI/litellm)).

It collects usage events from two sources into a single SQLite warehouse, then renders a
floating widget (PySide6) and a terminal UI showing real cost vs list-price cost.

![Token Ledger widget](docs/screenshot.png)

## Why

If you use multiple LLM backends (Claude Code for chat, a MiniMax-M3 / M2.7 model for code
generation, local Ollama models for embeddings), tracking real spend across all of them is
painful. This tool gives you a single pane of glass.

Two cost lenses, kept separate:

| Lens      | Meaning                                                    |
|-----------|------------------------------------------------------------|
| **List-price** | What the same tokens would cost at public API rates. Useful as a relative meter. |
| **Real**       | What you actually pay: subscription flat fees amortized daily + pay-per-use where applicable. |

## Features

- **Multi-source collection**: pulls from Claude Code JSONL transcripts (`~/.claude/projects/*.jsonl`)
  and from a LiteLLM Postgres container (`LiteLLM_SpendLogs`).
- **Model attribution**: if you run a local proxy that remaps model names (e.g. an Anthropic
  `claude-sonnet-4-6` request that gets forwarded to a MiniMax-M3 backend), the original model
  is recorded in a sidecar and re-attributed in the ledger.
- **Floating widget**: PySide6 frameless window, draggable, always-on-top. Filters by period
  (`today` / `7d` / `30d` / `3mo` / `all`) and provider (`All` / `Claude` / `MiniMax` / `Local`).
- **Terminal live view**: TUI mode (`tokenstats live`).
- **Idempotent collection**: replays are safe. Deduplication on event UUIDs.
- **No cloud, no telemetry**: everything is local SQLite + your local files.

## Install

```bash
git clone https://github.com/<your-user>/token-ledger
cd token-ledger
pip install PySide6
```

You also need either:
- **Claude Code** with transcripts at `~/.claude/projects/*.jsonl` (default), **or**
- **LiteLLM** running in Docker with `litellm-db` container accessible via `docker exec`.

You can disable either source — see [Configuration](#configuration).

## Usage

```bash
# collect (cron every 5 min, or hook on session end)
python3 collect.py

# quick mode (claude_code only, used as a SessionEnd hook)
python3 collect.py --quick

# floating widget
python3 card.py

# terminal live view
python3 live.py
```

### Sample dashboard

The included `seed_demo.py` populates `ledger.db` with 30 synthetic events so you can try
the widget immediately:

```bash
python3 seed_demo.py
python3 card.py
```

## Configuration

Edit `prices.json`:

```jsonc
{
  "_order": ["opus", "sonnet", "haiku", "minimax", "default"],
  "prices": {
    "opus":    {"input": 15.0, "output": 75.0, "cache_read": 1.5,  "cache_write": 18.75},
    "sonnet":  {"input": 3.0,  "output": 15.0, "cache_read": 0.3,  "cache_write": 3.75},
    "haiku":   {"input": 1.0,  "output": 5.0,  "cache_read": 0.1,  "cache_write": 1.25},
    "minimax": {"input": 0.3,  "output": 1.1,  "cache_read": 0.0,  "cache_write": 0.0},
    "default": {"input": 0.0,  "output": 0.0,  "cache_read": 0.0,  "cache_write": 0.0}
  },
  "subscriptions": [
    {"name": "Claude Max 20x", "monthly_usd": 200.0, "keys": ["opus", "sonnet", "haiku"]},
    {"name": "MiniMax Pro",    "monthly_usd": 50.0,  "keys": ["minimax"]}
  ]
}
```

- `prices` — list-price USD per 1M tokens. First matching key wins (substring on model name).
- `subscriptions` — flat monthly fees. The widget amortizes them daily and assigns them as
  "real" cost to any model whose name matches one of the keys. Models without a match fall
  back to pay-per-use at the list-price.

### Optional: router model sidecar

If you run a local proxy that remaps model names, set `ROUTER_SIDECAR` in `collect.py` to
point at a JSONL file where each line is `{"ts": <epoch>, "chat": "<id>", "orig": "<original>", "final": "<remapped>"}`.
The collector will re-attribute remapped events to the original model.

## File layout

```
token-ledger/
├── collect.py        # SQLite writer, idempotent, --quick / --selftest modes
├── card.py           # PySide6 floating widget
├── live.py           # Terminal UI (Rich)
├── retro_minimax.py  # One-shot script to export pending historical corrections
├── seed_demo.py      # Generate 30 fake events to try the widget
├── prices.json       # Pricing table
├── README.md
└── docs/
    └── screenshot.png
```

## License

MIT.