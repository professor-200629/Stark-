# STARK — 3-minute demo script

**Do not tour the tabs.** You have more functionality than fits in three minutes; showing all of
it makes none of it land. This is the sequence, and nothing else.

**Setup before recording:** `python -m app.seed`, open `http://127.0.0.1:8000` in **Chrome or
Edge** (voice input needs one of those), grant microphone permission once, confirm the header
reads `facts 176` and `observations 7`. Have the Teach-it tab pre-filled — it is by default.

---

### 1 · The problem (0:00 – 0:15)

The first demo alert is already loaded. Don't touch anything yet.

> "It's 3am. A pager goes off on the payments API. The person on call tonight is not the person
> who fixed this last time — and there *was* a last time. Three of them."

### 2 · Without memory (0:15 – 0:35)

Click **Run comparison**. Point at the left panel.

> "Left is what an AI assistant gives you today. Check your deploys. Look at dashboards. Scale the
> service. It isn't wrong — it's just what anyone would say. Confidence low, zero citations."

### 3 · With Hindsight (0:35 – 0:55)

Point at the right panel.

> "Same alert, same model, with this team's memory. It's identified the failure family in one
> shot, it's citing three incidents by ID, and it estimates 52 minutes — the median of the three
> times this actually happened."

### 4 · The three incidents (0:55 – 1:10)

Point at **Similar past incidents**, then scroll to **Memories recalled**.

> "INC-1042, INC-1088, INC-1131. And these are the memories it pulled — keyword, semantic, graph
> and temporal retrieval each contributing, plus a consolidated observation backed by three
> incidents. Nobody wrote that observation. It was derived."

### 5 · Ask STARK why — voice (1:10 – 1:30)

Click the mic. Say: **"STARK, why?"**

STARK speaks the causes with their evidence. Then say: **"STARK, what should I avoid?"**

> "That's the part I care about."

### 6 · The dead ends (1:30 – 1:50)

Let it finish speaking, then point at the **Do not do** list on screen.

> "Three warnings, each from a specific past incident. Don't suspect Aurora — writer CPU was only
> 44% in INC-1131. Don't roll back today's deploy — the team almost did that in INC-1088 and the
> deploy was innocent.
>
> No runbook says this. It comes from the false-lead field in the postmortems. Knowing what *not*
> to chase at 3am is the scarcest knowledge in incident response, and it's the first thing lost."

### 6b · Your call, not STARK's (1:50 – 2:10)

Scroll to **Recommended actions — your call**. Type a reason into the top one and click **Reject**.

> "STARK proposes. I decide. Nothing restarts a database because a language model felt confident.
> And watch what it does with my reason — it writes it to memory as an experience fact.
>
> Approve the second one, confirm it worked, and re-run the same alert: the confirmed fix now
> leads, and the one I rejected is pushed to the bottom carrying the reason I rejected it. It's
> learning what this team trusts, not just what happened."

*(If you are tight on time, cut this beat — but it is the strongest architectural idea in the
project, so cut beat 4 instead.)*

### 7 · It refuses (2:10 – 2:25)

Click the fourth demo chip, **Novel — no matching history**. Click **Run comparison**.

> "A failure this team has never had. A memory system that pattern-matches something irrelevant
> here is worse than no memory at all — you've sent a sleep-deprived engineer down the wrong path
> with false authority. So it says: no precedent. Treat it as novel."

### 8 · Teach it (2:25 – 2:40)

**Teach it** tab → **Retain to memory**. Back to **Triage**, same novel chip, **Run comparison**.

> "The incident gets resolved, the responder records what happened, and it's memory instantly.
> Same alert — now grounded, citing INC-1161, knowing the fix, and knowing twenty minutes got
> wasted on firewall rules."

### 9 · The evidence (2:40 – 2:55)

**Learning** tab.

> "This isn't a vibe. We replay the whole incident history chronologically against an empty
> memory, scoring each incident using only what existed before it. Coverage goes zero to 43%. When
> a precedent existed, it found it nine times out of nine. On alerts with no precedent it stayed
> quiet nine times out of twelve.
>
> And I'll be precise: this is a replay evaluation on a synthetic corpus. Every formula is on
> screen and derived in METRICS.md. It measures retrieval, not production outcomes."

### 10 · Close (2:55 – 3:10)

> "In this corpus, the first time a team meets a failure family it takes 72 minutes. A repeat,
> when someone remembers, takes 43. STARK makes that happen on the first repeat, for whoever is
> holding the pager — not whoever held it last time.
>
> The next incident doesn't start from zero."

---

## Voice commands

| Say | STARK does |
|---|---|
| "STARK, investigate this alert" | Runs the comparison and speaks the brief |
| "STARK, why?" | Reads the likely root causes with their evidence |
| "STARK, what should I avoid?" | Reads the dead ends and which incident each cost time in |
| "STARK, next alert" | Loads the next demo alert |
| "STARK, repeat that" | Replays the last spoken brief |
| "STARK, stop" | Cancels playback |

The recogniser mishears "STARK" as "start", "spark" or "stock" constantly — all of those are
accepted as the wake word. If the microphone fails during the demo, **Speak brief** does the same
job from a button, and everything else works without voice.

---

## If a judge asks

**"How did you get 43% coverage?"**
`true_positive / total_incidents = 9 / 21`. An incident counts as a true positive when the
grounding gate passes *and* a same-family incident is in the top 3. Chronological replay, scored
before each incident is retained, so there's no lookahead. It's on screen under the chart and in
METRICS.md.

**"Is the MTTR drop real?"**
It's a property of the synthetic corpus, not a result STARK produced — it's the motivation, not
the evidence. Median 72.5 to 43 minutes, a 40.7% drop. The mean says 58.8% but three long-tail
first-time incidents drag it, so we lead with the median.

**"Is the data real?"**
Synthetic, deliberately, and labelled as such everywhere. Real error signatures, named responders,
recurring failure families. INC-1149 looks exactly like INC-1055 but has a different root cause and
the responder wasted 14 minutes applying the old fix — the corpus punishes naive pattern matching
on purpose.

**"What if the LLM is down?"**
The brief is composed directly from recalled memory. Fewer nice sentences, identical citations and
warnings. The `synthesis` badge on each panel tells you which path ran.

**"Where is Hindsight actually used?"**
`retain` decomposes each incident into world facts and experience facts with metadata. `recall`
does the four-way search. Observations consolidate repeats into beliefs with proof counts. Mental
models hold curated playbooks that outrank raw facts. `demo/HINDSIGHT_MEMORY.md` maps every claim
to a function.

**"Is it autonomous? Would you let it touch production?"**
No, and that's deliberate. Every recommendation carries a risk level derived from the verb, and a
human approves, rejects, or defers it. The ruling is retained to memory, so STARK accumulates a
record of which of its own recommendations this team trusted and whether they worked. Autonomy
without that record is the thing nobody should ship.

**"Would someone pay for this?"**
Every company with an on-call rotation already pays for it, in minutes. One avoided SEV1 covers a
year of the product.
