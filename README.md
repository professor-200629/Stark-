# STARK

### On-call incident intelligence that remembers.

An incident response agent that gets better at your outages every time you have one.
Built on [Hindsight](https://hindsight.vectorize.io) — memory for AI agents.

---

## The problem

At 03:00 a pager goes off. The responder is not the person who fixed this last time. They open
Grafana, then the runbook, then Slack, then the last three postmortems, and spend the first
twenty minutes re-deriving a diagnosis their own team already paid for — sometimes by repeating
the exact false lead that cost the last responder fifteen minutes.

The knowledge exists. It is just scattered across postmortem docs nobody reads and the heads of
people who are asleep.

In the incident corpus shipped with this project, the numbers say it plainly:

| | Median time to resolve |
|---|---|
| First time the team saw a failure family | **72.5 min** |
| A repeat, once someone remembered the last one | **43 min** |

**A 41% drop, purely from having seen it before.** (By mean it is 59%, but three long-tail
first-time incidents drag the mean, so the median is the honest headline — see
[METRICS.md](METRICS.md).) This is a property of the corpus, not a result STARK produced: it is
the reason the product exists, not evidence that it works.

STARK's job is to make that drop happen on the *first* repeat rather than the third — and to make
it happen for the responder who was not there last time.

## What STARK does

Paste a raw alert. STARK fingerprints it, recalls every relevant thing the team has ever
learned, and returns a triage brief that cites its sources:

- **the past incidents this resembles**, with incident IDs and similarity
- **likely root causes**, each backed by the incidents that support it
- **first actions**, taken from the fixes that actually worked, with the observed MTTR
- **a "do NOT do" list** — the false leads that burned real minutes in past incidents
- **an honest "I have never seen this"** when memory holds no precedent
- **recommendations you rule on** — approve, reject, or investigate, with the reason remembered

You can also just ask it out loud — see [Voice](#voice) below.

Then, when the incident is resolved, the responder records what happened and it becomes memory
for the next person. The loop closes.

---

## Quickstart

**macOS / Linux**

```bash
cd stark
pip install -r requirements.txt
python -m app.seed          # load the incident corpus into memory
python -m uvicorn app.main:app
# open http://127.0.0.1:8000
```

Or just `./run.sh`.

**Windows (PowerShell)**

```powershell
cd stark
python -m pip install -r requirements.txt
python -m app.seed
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Or just `.\run.ps1`. Use `python -m uvicorn` rather than bare `uvicorn` — it
guarantees the project directory is on `sys.path`, which is what makes
`app.main:app` importable.

> Every command must be run **from inside the `stark` folder**. `app.main:app`
> is a path relative to the working directory, so running it from anywhere else
> gives `ModuleNotFoundError: No module named 'app'`.

**No API keys are required to run the demo.** STARK ships with a local memory engine and a
deterministic synthesiser so it boots and works immediately. Add credentials to upgrade:

```bash
cp .env.example .env
```

| Variable | Effect |
|---|---|
| `HINDSIGHT_BASE_URL` | Switches the memory layer to a real Hindsight server or Hindsight Cloud |
| `HINDSIGHT_API_KEY` | Bearer token for Hindsight Cloud |
| `GROQ_API_KEY` | Switches reasoning from the deterministic synthesiser to an LLM |

Nothing else changes. The agent code never imports a backend directly — it talks to one interface.

### Running against Hindsight Cloud

1. Sign up at [ui.hindsight.vectorize.io](https://ui.hindsight.vectorize.io) (promo code `MEMHACK89` for $50 credit, applied in Billing after registration).
2. Put the base URL and API key in `.env`.
3. `python -m app.seed` — STARK creates the memory bank with its mission, disposition and directives, then retains the corpus.

### Running Hindsight locally

```bash
pip install hindsight-api
export HINDSIGHT_API_LLM_API_KEY=$GROQ_API_KEY
hindsight-api                       # serves on :8888
# then, in .env:
HINDSIGHT_BASE_URL=http://localhost:8888
```

---

## The approval loop

STARK proposes. A person decides. Nothing restarts a database because a language model felt
confident.

Each grounded brief turns its first actions into recommendations carrying a stable key, a risk
level derived from the *verb* (`roll back` and `restart` are high risk; `check` and `inspect` are
low, regardless of how sure the model sounds), the evidence behind it, and the observed impact:

```
[high risk]  Roll back the retry endpoint, then re-deploy with the shared pool bean
             Resolved INC-1042 in 74 minutes.
             evidence: INC-1042, INC-1131
             [ Approve ]  [ Investigate first ]  [ Reject ]
```

Every decision is written back to Hindsight as an **experience fact**:

> *On 2026-08-10, priya rejected STARK's recommendation for payments-api
> (db-connection-pool-exhaustion): raise default_pool_size to 80. Reason given: PgBouncer is
> already at 80 in prod since INC-1131.*

That is a second learning signal, orthogonal to the incident history:

| | records |
|---|---|
| incident history | what the **system** did |
| decision history | what this **team** thinks STARK should do |

The next time a similar alert fires, the recall that surfaces past incidents surfaces past
decisions too — same retrieval path, different kind of fact — and recommendations are reranked by
what the team actually trusted. A confirmed success leads. A rejected action is **demoted but kept
visible**, carrying the reason it fell, because hiding it would throw away the useful part.

Two design choices worth defending:

- **Recommendation keys ignore tuned numbers.** `Raise default_pool_size to 20` and `…to 80` hash
  to the same key, so a track record accumulates across incidents instead of resetting every time
  someone changes a value.
- **A novel alert proposes nothing.** With no precedent, STARK has no business asking anyone to
  approve a production change, so the approval panel stays empty and the brief stands as advice
  only.

Code: `app/approvals.py`, `StarkAgent.record_decision`, `POST /api/decision`.

---

## Voice

Click the mic and say **"STARK, investigate this alert."** It runs the triage and reads the
briefing back:

> *"I found four relevant incidents. The strongest precedent is incident 1131 from December. Most
> likely cause: PgBouncer default_pool_size was left at 20 server connections per user/db pair.
> What resolved incident 1131 was: raise default_pool_size to 80 and max_client_conn to 5000. One
> thing not to repeat during incident 1131: briefly suspected Aurora, writer CPU was only 44%.
> That was a dead end. Similar incidents took about 52 minutes to resolve."*

| Say | STARK does |
|---|---|
| "STARK, investigate this alert" | Runs the comparison and speaks the brief |
| "STARK, why?" | Reads the likely root causes with their evidence |
| "STARK, what should I avoid?" | Reads the dead ends and which incident each cost time in |
| "STARK, next alert" | Loads the next demo alert |
| "STARK, repeat that" | Replays the last spoken brief |
| "STARK, stop" | Cancels playback |

Built on the browser's Web Speech API — no API key, no cost, no network round-trip for speech.
Input needs Chrome or Edge; playback works everywhere, and the **Speak brief** button does the
same job if the microphone fails mid-demo.

Written text reads badly out loud, so `app/voice.py` composes a separate spoken briefing: incident
IDs become "incident 1131", multi-clause runbook steps are cut at the first actionable clause, and
the whole thing is held under 110 words. Every triage response carries it as a `spoken` field.

---

## How Hindsight memory is used

Memory is not a lookup table bolted onto a chatbot here. It is the thing being demonstrated.

### 1. `retain` — incidents are decomposed, not dumped

Storing a postmortem as one blob makes it retrievable only as a blob. Each incident is broken into
atomic, independently-retrievable memories, split across Hindsight's two fact types:

| Memory | Type | Why the distinction matters |
|---|---|---|
| the alert that fired, its payload, the symptoms | **world fact** | objective system behaviour |
| the root cause, the verification, the impact | **world fact** | what turned out to be true |
| *"priya resolved this in 74 minutes by …"* | **experience fact** | something **this team did**, weighted differently at recall |
| *"22 minutes were wasted chasing a node upgrade"* | **experience fact** | the false lead — the highest-value memory in the entire system |

Every memory carries `incident_id`, `service`, `failure_class`, `severity`, `mttr_minutes` and the
fix as metadata, which is what lets flat search results be reassembled into incident cards later.

See `app/agent.py::incident_to_memory_items`.

### 2. `recall` — four strategies, then a second hop

An alert is fingerprinted first (service, alert name, log signatures, entities) rather than thrown
at search raw, because alert text is 80% threshold boilerplate. The fingerprint drives a recall
that blends **keyword**, **semantic**, **entity-graph** and **temporal** signals — Hindsight's TEMPR
model — so `AuthGateway401Spike` finds `auth-gateway` incidents even though the string never appears.

Then a **second retrieval hop**: once the top incidents are known, STARK goes back to memory and
asks specifically for *their* root cause, fix and false lead. This is the difference between "this
looks like INC-1149" and "here is exactly what INC-1149 turned out to be, and here is the thing
that wasted fourteen minutes last time."

See `app/agent.py::recall_for_alert` and `::enrich`.

### 3. Observations — consolidated, evidence-backed beliefs

Hindsight merges overlapping facts into deduplicated observations with a proof count and quoted
evidence. That is what lets the agent say:

> *payments-api has hit 'db-connection-pool-exhaustion' in 3 separate incidents. The resolution
> that worked most often (2/3) was PgBouncer in transaction pooling mode with a per-pod cap.
> Median time to resolve was 52 minutes.*

Nobody wrote that sentence. It was derived from the incident record.

### 4. Mental models — curated playbooks that outrank everything

Three team playbooks are installed as mental models and take priority over raw facts during
reasoning — e.g. *"never fail over Aurora while replay lag is climbing (INC-1080, INC-1137)"*.

### 5. Mission, directives, disposition

The bank is created with an SRE mission, a skeptical/literal disposition, and hard directives:
never invent an incident ID, always state how many incidents support a recommendation, say so
plainly when memory holds nothing. See `app/memory.py::BANK_MISSION`.

### 6. The grounding gate — the part most memory demos skip

An on-call agent that confidently cites the wrong incident is **worse than one with no memory at
all**. Before reasoning, STARK scores whether recalled incidents genuinely resemble the alert
(service match, entity overlap, distinctive-token overlap). If nothing clears the bar, memory is
withheld entirely and the agent says so.

The fourth demo alert exists specifically to show this working.

### 7. Write-back — the loop closes

`POST /api/outcome` retains the resolved incident immediately. The **Teach it** tab in the UI turns
this into a live demo: triage the novel webhook alert (agent says it has no precedent), record the
outcome, re-run the same alert, and it is now grounded with the fix and the false lead.

---

## Does it actually get better? A held-out evaluation

Claims about "learning over time" are easy to make and easy to fake. `GET /api/learning-curve`
replays the full incident history chronologically against an **empty** memory, scoring each
incident using only the memories that existed strictly before it. No lookahead.

```
coverage (incidents the agent could brief from memory)   0% → 43%   (9/21)
precedent found when one existed                         9/9
novel alerts correctly flagged as novel                  9/12
false positives                                          3, all a different failure family
                                                         on the same service
```

**These are replay-evaluation results on a synthetic corpus, not production measurements.** Every
formula, definition and caveat is returned in the `provenance` block of the API response, rendered
under the chart in the UI, and derived by hand in **[METRICS.md](METRICS.md)** — including why the
MTTR headline uses the median rather than the flattering mean.

Coverage starts at zero — there is nothing to remember yet — and climbs as failure families repeat.
That curve *is* the product.

The three false positives are worth being upfront about: each is a different failure family on the
*correct* service (e.g. a feature-flag rollout on `payments-api` recalling the pool-exhaustion
family). Retrieval never reached for an unrelated service. Tightening this further trades against
recall, and for on-call work recall is the more expensive side to lose.

---

## Architecture

```
        alert text
            │
            ▼
   ┌────────────────┐
   │  fingerprint   │  service · alert name · log signatures · entities
   └────────┬───────┘
            ▼
   ┌────────────────┐        ┌──────────────────────────┐
   │    recall      │───────▶│  Hindsight memory bank   │
   │  (TEMPR blend) │        │  world · experience      │
   └────────┬───────┘        │  observations · models   │
            ▼                └──────────────────────────┘
   ┌────────────────┐                    ▲
   │  second hop    │────────────────────┘
   │  enrich cards  │
   └────────┬───────┘
            ▼
   ┌────────────────┐
   │ grounding gate │  nothing relevant? say so, and stop.
   └────────┬───────┘
            ▼
   ┌────────────────┐
   │    reason      │  LLM constrained to cite recalled incidents
   └────────┬───────┘
            ▼
      triage brief ──▶ responder resolves ──▶ retain ──▶ (back to the bank)
```

| File | Responsibility |
|---|---|
| `app/memory.py` | Hindsight adapter + interface-compatible local engine, observations, entity extraction |
| `app/agent.py` | fingerprinting, recall, second-hop enrichment, grounding gate, briefs, write-back, evaluation |
| `app/llm.py` | Groq client with JSON recovery + deterministic fallback |
| `app/approvals.py` | recommendations, risk classification, decision log, track records, reranking |
| `app/voice.py` | composes the spoken briefing (ID normalisation, clause trimming, length budget) |
| `app/ledger.py` | structured MTTR bookkeeping (charts only — never used to answer a triage question) |
| `app/seed_data.py` | 21-incident corpus + 4 held-out demo alerts |
| `app/main.py` | FastAPI surface |
| `static/index.html` | dependency-free demo UI |

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/seed` | Load the incident corpus into memory (idempotent) |
| `POST /api/triage` | `{alert, use_memory}` → triage brief |
| `POST /api/compare` | Same alert, memory off vs on, side by side |
| `POST /api/recall` | Raw memory search — the inspector behind the UI |
| `POST /api/outcome` | Record a resolved incident; retains it immediately |
| `POST /api/decision` | Approve / reject / investigate a recommendation; retained to memory |
| `POST /api/decision/outcome` | Did the approved action actually work? |
| `GET /api/decisions` | Decision log and approval statistics |
| `GET /api/memory/observations` | Consolidated beliefs with proof counts |
| `GET /api/learning-curve` | Held-out chronological replay + MTTR analysis |
| `GET /api/incidents` | The corpus |
| `GET /api/status` | Backend mode, memory size, LLM mode |

## Proving the loop works

```bash
python verify_demo.py                                # in-process, no server needed
python verify_demo.py --url http://127.0.0.1:8000    # against a running server
```

32 checks walking both loops in order. The incident loop: seed, recall, grounded brief with
citations, the grounding gate refusing a novel alert, teaching STARK an outcome, and the same alert
now answered from that new memory. The decision loop: propose, reject one with a reason, approve
another and confirm it worked, then re-run and watch the ranking change. Plain-text output, meant
to be pasted to anyone who wants evidence rather than screenshots.

## Tests

```bash
pytest -q     # 73 tests
```

Covers entity extraction, observation consolidation and proof counts, fingerprinting, the grounding
gate on both sides (grounded *and* correctly-novel), the memory-off/memory-on delta, the write-back
loop making a previously-novel alert grounded, that the spoken briefing stays listenable and never
leaks a raw incident ID, that a rejected recommendation is demoted but never silently dropped, and
that the learning-curve replay never looks ahead.

## The data

The corpus is synthetic but written to behave like a real one: 21 incidents over 12 months at a
fictional payments company, with real-shaped error logs (`HikariPool-1 - Connection is not
available`, `SerializationException: Unknown magic byte!`, `OOMKilled  Exit Code: 137`), named
responders, real MTTRs, and — critically — **failure families that recur with different symptoms**.

Two details do most of the work. First, false leads are recorded, because knowing what *not* to
chase is the scarcest knowledge in incident response. Second, INC-1149 looks like INC-1055 (both
Kafka lag) but has a completely different cause, and the responder wasted 14 minutes applying the
old fix — so the corpus punishes naive pattern matching, and the agent has to be right for the
right reasons.

## License

MIT.
