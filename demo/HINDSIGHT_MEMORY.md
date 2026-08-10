# How Hindsight memory is used in STARK

*Submission requirement: an explicit account of the memory layer.*

## Summary

STARK is an on-call incident response agent. Hindsight is not a feature of it — it is the
substrate the product stands on. Remove memory and the agent collapses into a generic assistant
that suggests checking dashboards; that exact degradation is what `POST /api/compare` renders side
by side.

## Operation by operation

### `retain`

Each incident is decomposed into 7–8 atomic memories rather than stored as one document, because a
blob is only retrievable as a blob.

| Memory | Hindsight type |
|---|---|
| alert title, alert payload, symptoms | world fact |
| root cause, verification, customer impact | world fact |
| "*responder X resolved this in N minutes by …*" | experience fact |
| "*this false lead wasted N minutes*" | experience fact |

Every item carries `incident_id`, `service`, `failure_class`, `severity`, `mttr_minutes`, `fix` and
`tags` as metadata, plus the incident's real timestamp (so temporal retrieval is meaningful) and a
`document_id` of the incident ID.

The world/experience split is load-bearing: experience facts are the team's own actions, and they
carry higher priority at recall, so *what we did last time* outranks *what was true last time*.

Code: `app/agent.py::incident_to_memory_items`, `app/memory.py::MemoryStore.retain`.

### `recall`

Alerts are ~80% threshold boilerplate, so the raw text is a poor query. STARK fingerprints
first — service, alert name, log signatures, entities — then queries. Retrieval blends four
strategies in parallel:

| Strategy | What it catches here |
|---|---|
| keyword (BM25) | `HikariPool`, `OOMKilled`, `Unknown magic byte` |
| semantic | paraphrased symptom descriptions |
| entity graph | `AuthGateway401Spike` → `auth-gateway` → its incidents |
| temporal | recency weighting on a 120-day half-life |

A **second retrieval hop** then fetches root cause, fix and false lead for the incidents that
ranked highest. One hop tells you *which* incident; two hops tell you *what to do*.

Code: `app/agent.py::fingerprint`, `::build_recall_query`, `::recall_for_alert`, `::enrich`.

### Observations

Facts that repeat across incidents are consolidated into deduplicated beliefs carrying a proof
count and quoted evidence — e.g. *"payments-api has hit db-connection-pool-exhaustion in 3 separate
incidents; the resolution that worked most often (2/3) was PgBouncer transaction pooling; median
MTTR 52 minutes."* Nobody authored that; it was derived. Visible in the UI under **Memory →
Observations** and at `GET /api/memory/observations`.

### Mental models

Three curated team playbooks are installed as mental models and outrank raw facts during
reasoning — including *"never fail over Aurora while replay lag is climbing (INC-1080, INC-1137)"*
and *"do not restart or scale a Kafka consumer group mid-rebalance (INC-1055, INC-1104)."*

Code: `app/agent.py::_install_mental_models`.

### Mission, directives, disposition

The bank is created with an SRE mission, a skeptical and literal disposition (`skepticism: 4`,
`literalism: 4`), and hard directives — never invent an incident ID, always state how many
incidents support a recommendation, prefer the best observed outcome over the most recent one, and
say plainly when memory holds nothing.

Code: `app/memory.py::BANK_MISSION`, `BANK_DIRECTIVES`, `HindsightBackend.ensure_bank`.

### Grounding gate

Memory is only handed to the reasoning step if recalled incidents genuinely resemble the alert.
A confidently wrong citation at 3am is worse than silence, so the gate is strict and the fourth
demo alert exists to prove it fires.

Code: `app/agent.py::score_relevance`.

### Write-back — two loops, one memory bank

`POST /api/outcome` retains a resolved incident immediately, closing the loop within the demo
itself: a novel alert becomes a grounded one in about fifteen seconds of stage time.

`POST /api/decision` closes a second loop. When a human approves, rejects, or defers one of
STARK's recommendations, that ruling is retained as an **experience fact** in the same bank —
*"On 2026-08-10, priya rejected STARK's recommendation for payments-api: raise default_pool_size
to 80. Reason given: PgBouncer is already at 80 in prod since INC-1131."*

This matters for the Hindsight story specifically. Both loops write to one bank and are retrieved
by one recall. When an alert fires, the same four-way search that surfaces past incidents also
surfaces past decisions about them, because they are the same kind of object: things this team
experienced. A memory system that could only hold incidents would need a separate database and a
separate query path for decisions.

Recommendations are then reranked by that history — confirmed successes lead, rejected actions are
demoted but stay visible with the reason they fell.

Code: `app/approvals.py`, `StarkAgent.record_decision`, `StarkAgent._recall_prior_decisions`.

## Portability

`app/memory.py` defines one interface with two implementations: `HindsightBackend` (the official
`hindsight-client`) and `LocalMemoryEngine` (a dependency-free engine with the same semantics —
world/experience facts, observation consolidation, four-signal retrieval). Setting
`HINDSIGHT_BASE_URL` switches backends; no other line of code changes.

The local engine exists for two honest reasons: the demo must boot with zero credentials for
judges, and the Hindsight integration must be testable offline in CI. It is not a replacement —
it is a shim shaped like the real thing.

## Measured effect

`GET /api/learning-curve` replays all 21 incidents chronologically against an empty memory, scoring
each with only the memories that existed before it:

- coverage: **0% → 43%**
- precedent found when one existed: **9/9**
- novel alerts correctly flagged novel: **9/12**, and all 3 misses were a different failure family
  on the *correct* service
- MTTR in the underlying corpus: **107.6 min** first-of-family vs **44.3 min** on a repeat
