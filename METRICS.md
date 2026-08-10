# How every number in STARK is calculated

Short version: **every metric on the Learning page is a replay evaluation over a synthetic
corpus.** None of it is a production measurement. The corpus was written for this project; the
evaluation over it is real, reproducible, and runs on demand at `GET /api/learning-curve`.

If a judge asks where a number comes from, this page has the arithmetic.

---

## The data

| | |
|---|---|
| Source | `app/seed_data.py` — 21 incidents at a fictional payments company |
| Period | April 2025 – April 2026 |
| Nature | **Synthetic.** Written by hand to be structurally realistic: real error signatures, named responders, plausible MTTRs, and failure families that recur with different symptoms |
| Why synthetic | Real incident corpora are confidential. The structure that matters — repeated failure families, recorded false leads, differing root causes behind similar signatures — is preserved |

The one thing the corpus deliberately does *not* make easy: `INC-1149` has the same alert
signature as `INC-1055` (both Kafka lag) but a completely different root cause, and the responder
wasted 14 minutes applying the old fix. Naive pattern matching is penalised by construction.

---

## Metric 1 — coverage

**What it means:** of all incidents seen so far, the share the agent could correctly brief from
memory.

**How it is produced:** the corpus is sorted by `opened_at` and replayed against an empty memory,
one incident at a time. Each incident is scored **before** it is retained, so the agent only ever
sees memories that existed strictly earlier. No lookahead.

```
coverage = true_positive / total_incidents
         = 9 / 21
         = 43%
```

A `true_positive` requires two things: the grounding gate passed, **and** an incident of the same
`failure_class` appears in the top 3 recalled incidents.

Coverage starts at 0% and can never exceed the share of incidents that have a precedent
(9/21 = 43% here) — the first occurrence of any failure family is unbriefable by definition. The
curve reaching its ceiling means the agent recalled every precedent that existed.

Code: `app/agent.py::StarkAgent.learning_curve`.

---

## Metric 2 — recall when a precedent exists

**What it means:** when memory *did* contain a same-family incident, how often was it surfaced.

```
recall = true_positive / (true_positive + false_negative)
       = 9 / (9 + 0)
       = 100%
```

This is the metric that would degrade first if retrieval were weak. It is 9/9 on this corpus.
That is a small denominator, and it should be quoted with the denominator attached — "9 out of 9",
not "100%".

---

## Metric 3 — behaviour on novel alerts

**What it means:** when there was no precedent, did the agent correctly decline to pattern-match.

```
false positive rate = false_positive / (false_positive + true_negative)
                    = 3 / (3 + 9)
                    = 25%
```

So 9 of 12 novel incidents were correctly flagged novel.

**All 3 false positives were a different failure family on the *correct* service** —
a feature-flag rollout on `payments-api` recalling the pool-exhaustion family, a frontend
regression on `checkout-web` recalling the CDN family, and a poison-message loop on
`ledger-worker` recalling the consumer-lag family. Retrieval never reached for an unrelated
service. That is reported as `confusion.false_positive_same_service` in the API response.

Tightening this trades directly against recall. For on-call work, recall is the more expensive
side to lose, so the gate is tuned accordingly — and that is a choice, not an accident.

---

## Metric 4 — MTTR, first-time vs repeat

**This one is a property of the corpus, not of the agent.** It is the motivating statistic — it
shows why memory is worth building — and it must not be presented as a result STARK produced.

An incident is **first of family** if no earlier incident in the corpus shares its
`failure_class`; otherwise it is a **repeat**.

```
first of family (n=12):  11, 41, 57, 63, 66, 71, 74, 88, 96, 194, 210, 320   median 72.5   mean 107.6
repeats         (n=9):   26, 29, 31, 34, 43, 47, 52, 58, 79                  median 43.0   mean  44.3
```

```
median reduction = (72.5 - 43.0) / 72.5 = 40.7%     <- headline
mean   reduction = (107.6 - 44.3) / 107.6 = 58.8%
```

**Lead with the median: 40.7%.** Three first-of-family incidents are long-tail (194, 210 and 320
minutes — a duplicate-invoice data bug, a poison-message loop with no alert coverage, and a
bundle-size regression nobody paged on). They drag the mean up. Quoting 58.8% without that caveat
would be picking the flattering statistic.

Both figures, both raw series, and the derivation strings are returned by the API so the
arithmetic can be checked by hand.

Code: `app/ledger.py::IncidentLedger.mttr_summary`.

---

## What STARK does *not* claim

- No claim that STARK reduced anyone's real MTTR. It has never run against a production incident.
- No claim of statistical significance. n=21 incidents, n=9 repeats. These are demonstration-scale
  numbers.
- No claim that the LLM's reasoning quality was evaluated. The replay measures **retrieval** —
  whether the right past incident was surfaced — not whether the generated prose was good.
- No benchmark comparison against other memory systems. Not attempted.

## Reproducing everything

```bash
python -m app.seed
python -m uvicorn app.main:app
curl localhost:8000/api/learning-curve | python -m json.tool
```

The response includes a `provenance` block carrying the method, the definition of every confusion
term, the formula for every rate, and an explicit synthetic-data caveat. `pytest -q` asserts the
replay never looks ahead (`test_learning_curve_never_looks_ahead`).
