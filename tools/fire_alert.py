"""
Fire a real HTTP webhook at a running STARK, the way a monitoring system would.

    python tools/fire_alert.py                      # Alertmanager payload
    python tools/fire_alert.py datadog              # a different vendor shape
    python tools/fire_alert.py --file my_alert.json # your own payload
    python tools/fire_alert.py --list               # show the bundled samples

This exists so the demo can show the incident *arriving* rather than being pasted.
It posts over the network to prove the endpoint works outside the test client.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import SAMPLE_PAYLOADS  # noqa: E402


def post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", default="alertmanager",
                        help=f"one of {sorted(SAMPLE_PAYLOADS)}")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api/webhook/alert")
    parser.add_argument("--file", help="path to a JSON payload of your own")
    parser.add_argument("--list", action="store_true", help="list bundled samples and exit")
    args = parser.parse_args()

    if args.list:
        for name, payload in SAMPLE_PAYLOADS.items():
            print(f"\n=== {name} ===")
            print(json.dumps(payload, indent=2)[:600])
        return 0

    if args.file:
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    elif args.source in SAMPLE_PAYLOADS:
        payload = SAMPLE_PAYLOADS[args.source]
    else:
        print(f"Unknown source {args.source!r}. Try one of {sorted(SAMPLE_PAYLOADS)}.")
        return 2

    print(f"POST {args.url}")
    try:
        result = post(args.url, payload)
    except urllib.error.URLError as exc:
        print(f"\nCould not reach STARK: {exc}")
        print("Is the server running?  python -m uvicorn app.main:app")
        return 1

    received, summary = result["received"], result["summary"]
    print(f"\n  parsed as   {received['source']}")
    print(f"  service     {received['service'] or '(not stated)'}")
    print(f"  title       {received['title']}")
    print(f"\n  {'GROUNDED' if summary['memory_grounded'] else 'NOVEL'} · confidence {summary['confidence']}"
          + (f" · est. {summary['estimated_mttr_minutes']} min" if summary["estimated_mttr_minutes"] else ""))
    print(f"  {summary['verdict']}")
    if summary["cited"]:
        print(f"  cites       {', '.join(summary['cited'])}")
    if summary["recommendations"]:
        print(f"  proposes    {summary['recommendations']} actions awaiting a human decision")
    print("\n  Open http://127.0.0.1:8000 and check the Inbox tab.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
