"""CLI: load the incident corpus into memory.  python -m app.seed"""

from __future__ import annotations

import json
from pathlib import Path

from .agent import StarkAgent
from .config import settings
from .ledger import IncidentLedger
from .memory import MemoryStore


def main() -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(settings)
    ledger = IncidentLedger(Path(settings.state_dir) / "ledger.json")
    agent = StarkAgent(store, ledger)

    print(f"Memory backend: {store.mode}" + (f" ({settings.hindsight_base_url})" if store.mode == "hindsight" else ""))
    result = agent.seed()
    print(json.dumps(result, indent=2))
    print("\nMTTR: first-of-family vs repeat")
    print(json.dumps(ledger.mttr_summary(), indent=2))


if __name__ == "__main__":
    main()
