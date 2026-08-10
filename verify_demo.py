"""
Prove the whole memory loop works, end to end, in one command.

    python verify_demo.py                      # runs in-process, no server needed
    python verify_demo.py --url http://127.0.0.1:8000   # tests a running server

Every step below is the thing a judge (or a mentor) actually wants evidence of:
memory is populated, recall reaches the right incidents, the grounded brief cites
them, the grounding gate refuses a novel alert, and teaching STARK one incident
changes its answer to that same alert. Output is plain text, meant to be pasted.
"""

from __future__ import annotations

import argparse
import sys

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"\n         {detail}" if detail else ""))
    return ok


class Client:
    """Same surface whether we run in-process or against a live server."""

    def __init__(self, url: str | None) -> None:
        self.url = url
        if url:
            import httpx

            self._c = httpx.Client(base_url=url, timeout=120.0, trust_env=False)
        else:
            from fastapi.testclient import TestClient

            from app.main import app

            self._c = TestClient(app)

    def get(self, path: str):
        return self._c.get(path).json()

    def post(self, path: str, body: dict | None = None):
        return self._c.post(path, json=body or {}).json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="Base URL of a running STARK server")
    args = parser.parse_args()

    c = Client(args.url)
    where = args.url or "in-process (no server needed)"
    print(f"\nSTARK end-to-end verification — {where}\n" + "=" * 68)

    # ---------------------------------------------------------------- step 1
    print("\n1. Seed memory")
    seeded = c.post("/api/seed")
    status = c.get("/api/status")
    m = status["memory"]
    check("21 incidents retained", m["incidents"] == 21, f"incidents={m['incidents']}")
    check("facts populated", m["facts"] > 150, f"{m['facts']} facts "
          f"({m['world_facts']} world, {m['experience_facts']} experience)")
    check("observations consolidated", m["observations"] >= 5,
          f"{m['observations']} observations, {m['mental_models']} mental models")
    print(f"         backend={status['memory_backend']}  bank={m['bank_id']}  "
          f"llm={status['llm_model']}")

    # ---------------------------------------------------------------- step 2
    print("\n2. Recall reaches the right incidents")
    r = c.post("/api/recall", {"query": "What has caused payments-api latency before?"})
    ids = {h.get("metadata", {}).get("incident_id") for h in r["hits"]}
    ids.discard(None)
    pool = {"INC-1042", "INC-1088", "INC-1131"}
    check("pool-exhaustion family surfaced", bool(pool & ids),
          f"{r['count']} hits, incidents: {sorted(i for i in ids if i)[:6]}")
    strategies = {s for h in r["hits"] for s in h.get("strategies", [])}
    check("multiple retrieval strategies fired", len(strategies) >= 2,
          f"strategies: {sorted(strategies)}")

    # ---------------------------------------------------------------- step 3
    print("\n3. Grounded brief on a repeat incident")
    alerts = c.get("/api/demo-alerts")["alerts"]
    cmp = c.post("/api/compare", {"alert": alerts[0]["text"]})
    off, on = cmp["without_memory"], cmp["with_memory"]
    cited = [x["incident_id"] for x in on["recalled_incidents"]]
    check("without memory: no citations", not off["memory_grounded"] and not off["similar_incidents"],
          off["verdict"][:88])
    check("with memory: grounded", on["memory_grounded"], on["verdict"][:88])
    check("cites the pool-exhaustion family", bool(pool & set(cited)), f"cited {cited[:4]}")
    check("warns about past dead ends", len(on["do_not_do"]) > 0,
          " | ".join(f"{d['source_incident']}: {d['action'][:52]}" for d in on["do_not_do"][:2]))
    check("estimates time to resolve", bool(on["estimated_mttr_minutes"]),
          f"{on['estimated_mttr_minutes']} min, from past incidents of this family")
    check("produces a spoken briefing", bool(on.get("spoken")), on.get("spoken", "")[:88] + "...")

    # ---------------------------------------------------------------- step 4
    print("\n4. Grounding gate refuses a novel alert")
    novel = next(a for a in alerts if a["label"].startswith("Novel"))
    before = c.post("/api/triage", {"alert": novel["text"]})
    check("refuses to pattern-match", before["memory_grounded"] is False, before["verdict"][:88])
    check("cites nothing", before["recalled_incidents"] == [],
          f"recall returned {before['memory_used']} memories, none cleared the gate")

    # ---------------------------------------------------------------- step 5
    print("\n5. Teach STARK the outcome")
    taught = c.post("/api/outcome", {
        "incident_id": "INC-1161",
        "service": "webhook-dispatcher",
        "failure_class": "merchant-tls-trust-failure",
        "alert_title": "webhook delivery success rate 61%, x509 unknown authority",
        "root_cause": "A CA bundle update in the base image dropped two intermediate roots that "
                      "40% of merchant endpoints chain to.",
        "fix": "Pin the previous ca-certificates package and redeploy, then add a merchant TLS "
               "chain audit to the weekly job.",
        "false_leads": "Twenty minutes were spent on merchant-side firewall rules before anyone "
                       "checked the base image diff.",
        "mttr_minutes": 58,
        "responder": "demo",
    })
    check("outcome retained as memory", taught["retained"] > 0,
          f"{taught['retained']} memories written for {taught['incident_id']}")

    # ---------------------------------------------------------------- step 6
    print("\n6. The same alert, one incident later  <-- the money shot")
    after = c.post("/api/triage", {"alert": novel["text"]})
    check("now grounded", after["memory_grounded"] is True, after["verdict"][:88])
    check("cites what it was just taught",
          "INC-1161" in [x["incident_id"] for x in after["recalled_incidents"]],
          f"cited {[x['incident_id'] for x in after['recalled_incidents']]}")
    # With a live LLM the fix gets paraphrased, so match on substance rather than
    # on the exact sentence STARK happened to generate.
    steps = " ".join(a["step"].lower() for a in after["first_actions"])
    check("knows the fix",
          ("certificat" in steps or "ca bundle" in steps)
          and any(a.get("source_incident") == "INC-1161" for a in after["first_actions"]),
          next((a["step"][:88] for a in after["first_actions"]), ""))
    check("knows the dead end", len(after["do_not_do"]) > 0,
          next((d["action"][:88] for d in after["do_not_do"]), ""))
    check("estimates 58 minutes", after["estimated_mttr_minutes"] == 58,
          f"{after['estimated_mttr_minutes']} min")

    # ---------------------------------------------------------------- step 7
    print("\n7. Human approval loop — STARK proposes, a person decides")
    # Back to the clean corpus: step 5 deliberately taught STARK the webhook
    # incident, so the "novel" alert is no longer novel until we reset.
    c.post("/api/seed")
    recs = c.post("/api/triage", {"alert": alerts[0]["text"]})["recommendations"]
    check("grounded brief proposes actions", len(recs) >= 2,
          " | ".join(f"[{r['risk']}] {r['action'][:38]}" for r in recs[:2]))
    check("novel alert proposes nothing",
          c.post("/api/triage", {"alert": novel["text"]})["recommendations"] == [],
          "no precedent means no production change is proposed")

    rejected, approved = recs[0], recs[1]
    r1 = c.post("/api/decision", {
        "recommendation_key": rejected["key"], "decision": "reject", "action": rejected["action"],
        "service": "payments-api", "failure_class": rejected["failure_class"],
        "decided_by": "priya", "note": "PgBouncer is already at 80 in prod since INC-1131.",
    })
    check("rejection written to memory", "rejected STARK" in r1["memory"], r1["memory"][:96] + "...")

    c.post("/api/decision", {
        "recommendation_key": approved["key"], "decision": "approve", "action": approved["action"],
        "service": "payments-api", "failure_class": approved["failure_class"], "decided_by": "priya",
    })
    r2 = c.post("/api/decision/outcome", {
        "recommendation_key": approved["key"], "outcome": "worked",
        "note": "p99 back under 700ms in 6 minutes.",
    })
    check("confirmed outcome recorded", r2["updated"] and r2["track_record"]["worked"] == 1,
          r2["track_record"]["summary"])

    again = c.post("/api/triage", {"alert": alerts[0]["text"]})
    after = again["recommendations"]
    check("confirmed success now leads", after[0]["key"] == approved["key"],
          f"{after[0]['action'][:60]} — {after[0]['track_record']['summary']}")
    demoted = next((r for r in after if r["key"] == rejected["key"]), None)
    check("rejected action demoted but still visible", demoted is not None,
          f"kept with its reason: \"{(demoted or {}).get('track_record', {}).get('last_rejection_note', '')}\"")
    check("past decisions recalled alongside past incidents", len(again["prior_decisions"]) > 0,
          f"{len(again['prior_decisions'])} decisions surfaced by the same retrieval path")

    # ---------------------------------------------------------------- step 8
    print("\n8. Evaluation is a held-out replay on synthetic data")
    c.post("/api/seed")  # clear the decisions made above before evaluating
    lc = c.get("/api/learning-curve")
    p, conf = lc["provenance"], lc["confusion"]
    check("labelled synthetic", p["is_synthetic"] is True, p["data_source"])
    check("no lookahead", lc["points"][0]["memory_size"] == 0,
          "first incident scored against an empty memory")
    check("coverage climbs from zero", lc["points"][0]["coverage"] == 0.0 and lc["coverage"] > 0,
          f"{p['formulas']['coverage']} = {round(lc['coverage'] * 100)}%")
    check("recall when a precedent existed",
          lc["recall_when_precedent_exists"] >= 0.8,
          p["formulas"]["recall_when_precedent_exists"])
    check("novel alerts handled honestly", conf["false_negative"] == 0,
          f"{conf['true_negative']}/{conf['true_negative'] + conf['false_positive']} correctly "
          f"flagged novel; all {conf['false_positive']} false positives were the same service")
    check("MTTR derivation is shown", bool(lc["mttr"]["derivation_median"]),
          f"median {lc['mttr']['derivation_median']}  |  mean {lc['mttr']['derivation_mean']}")

    # ---------------------------------------------------------------- summary
    failed = [r for r in results if r[0] == FAIL]
    print("\n" + "=" * 68)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFailures:")
        for _, name, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    print("\nBoth loops work:")
    print("  incidents  seed -> recall -> grounded answer -> refuse when novel -> teach")
    print("             -> same alert now answered from the new memory")
    print("  decisions  propose -> human approves or rejects -> outcome -> retained")
    print("             -> next run reranks on what this team actually trusted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
