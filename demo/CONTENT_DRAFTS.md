# Content deliverable drafts

Starting points for the required article / social post / video. Rewrite in your own voice —
these read better once they sound like a person.

---

## Article draft (~800 words)

### The most valuable thing in your postmortems is the part nobody reads

Every incident postmortem has a section that gets filled in honestly and then never looked at
again. It is not the root cause. It is the false lead.

*"The first 22 minutes were spent chasing a Kubernetes node upgrade that had landed the same
evening. It was unrelated."*

That sentence is worth more at 3am than the entire rest of the document, and it is the first thing
that evaporates. The root cause makes it into a runbook. The wrong turn does not.

I spent this hackathon building **STARK**, an on-call agent whose entire job is to not lose
that sentence.

#### The gap

On-call rotations have a structural problem: the person paged is usually not the person who fixed
this last time. The knowledge exists — it is in a postmortem doc, a Slack thread, and the head of
someone who is asleep. So the responder rebuilds a diagnosis their own team already paid for.

You can see the cost in the data. In the incident corpus I built for this project, the first time a
team hit a given failure family it took a mean of 107 minutes to resolve. A repeat, when somebody
happened to remember the last one, took 44. Roughly a 59% drop, purely from recall.

The goal is to make that drop happen on the *first* repeat, for the person who was not there.

#### Why RAG over postmortems is not enough

The obvious approach is to embed your postmortems and do similarity search. It gets you
disappointingly little, for three reasons.

**A postmortem is only retrievable as a blob.** You want the fix for INC-1042, not eight paragraphs
about INC-1042.

**Alert text is 80% boilerplate.** Thresholds, durations, dashboard links. Embed it raw and you
retrieve on the boilerplate.

**Similarity is not sufficiency.** Knowing an alert resembles INC-1042 does not tell you what to do.

This is where [Hindsight](https://hindsight.vectorize.io) earned its place. It is a memory system
for agents rather than a vector store, and three of its ideas map cleanly onto incident response.

#### One: facts have kinds

Hindsight distinguishes **world facts** (things that were true) from **experience facts** (things
the agent or team did). So an incident decomposes into: the alert, the symptoms, the root cause —
and separately, *"priya resolved this in 74 minutes by rolling back the retry endpoint"* and
*"22 minutes were wasted on an unrelated node upgrade."*

That split turns out to be load-bearing. Experience facts are weighted higher at recall, so *what
we did last time* outranks *what was true last time*. It is also what makes a "do NOT do" section
possible at all.

#### Two: retrieval is four things at once

Hindsight blends keyword, semantic, entity-graph and temporal retrieval. In practice that means
`AuthGateway401Spike` finds `auth-gateway` incidents through the entity graph even though the
hyphenated string never appears in the alert.

I added a second hop on top: once the top incidents are known, go back and ask specifically for
*their* root cause, fix and false lead. One hop tells you which incident. Two hops tell you what to
do.

#### Three: repeated facts consolidate into beliefs

After enough incidents, Hindsight merges overlapping facts into observations with a proof count.
The agent ends up able to say:

> *payments-api has hit db-connection-pool-exhaustion in 3 separate incidents. The resolution that
> worked most often (2/3) was PgBouncer in transaction pooling mode. Median MTTR 52 minutes.*

Nobody wrote that sentence. It was derived.

#### The part I would defend hardest

An incident agent that confidently cites the wrong past incident is **worse than one with no memory
at all** — you have just sent a sleep-deprived engineer down a wrong path with false authority.

So before reasoning, STARK scores whether the recalled incidents genuinely resemble the alert.
If nothing clears the bar, memory is withheld and the agent says: no precedent, treat this as
novel. One of the four demo alerts exists purely to show this firing.

Building the *refusal* took longer than building the recall.

#### Proving "it learns" without hand-waving

Every memory demo claims the agent gets better. Most show a cherry-picked before and after.

STARK replays its full incident history chronologically against an empty memory, scoring each
incident using only the memories that existed strictly before it. No lookahead.

- Coverage — the share of incidents it could correctly brief from memory — climbs from 0% to 43%.
- When a precedent existed, it surfaced it 9 times out of 9.
- On incidents with no precedent, it correctly stayed quiet 9 times out of 12. All three misses
  were a different failure family on the *correct* service.

Coverage starting at zero is not a bug in the chart. There is nothing to remember yet. That curve
is the product.

#### What I would build next

Ingest real postmortems and Slack incident channels instead of a synthetic corpus. Push briefs into
the incident channel automatically when a page fires. And track whether the agent's suggested fix
was the one that actually worked — feed that back so the memory learns which of its own
recommendations are worth trusting.

Code: `<your repo link>`

---

## LinkedIn post

> Every incident postmortem contains one sentence that would save the next responder twenty
> minutes — and it is never the root cause.
>
> It is the false lead. *"The first 22 minutes were spent chasing a Kubernetes node upgrade that
> was unrelated."*
>
> Root causes make it into runbooks. Wrong turns don't. So the next person — who is usually not
> the person who fixed it last time — takes the same wrong turn.
>
> This weekend I built **STARK** — on-call incident intelligence that remembers. It holds every
> incident a team has run, including the dead ends, and you can just ask it out loud. Paste an alert and it tells you which past incidents this resembles,
> what actually fixed them, how long it took — and what not to chase.
>
> Built on Hindsight (@Vectorize) for the memory layer.
>
> Two things I'd point at:
>
> → It refuses. If nothing in memory genuinely matches, it says "no precedent" instead of
> pattern-matching something irrelevant. A confidently wrong citation at 3am is worse than silence,
> and building the refusal took longer than building the recall.
>
> → The learning claim is measured, not asserted. Replaying the incident history chronologically
> against an empty memory, scoring each incident with only what existed before it: coverage goes
> 0% → 43%, and it found the precedent 9/9 times when one existed.
>
> In the corpus, first-time-seen failures took 108 minutes to resolve. Repeats took 44. STARK
> exists to make that drop happen on the first repeat, for the person who wasn't there.
>
> Repo + 90-second demo: <link>
>
> #AIAgents #SRE #IncidentResponse #Hindsight

## X / Twitter thread

1/ Every postmortem has one sentence worth more than the rest at 3am. It's not the root cause. It's
the false lead — the wrong turn that cost 20 minutes. Runbooks capture root causes. Nothing
captures wrong turns. So the next responder takes the same one.

2/ Built **STARK** this weekend — on-call incident intelligence that remembers. Every incident your
team has run: the alert, the fix, the MTTR, the dead ends. Paste an alert or just say "STARK,
investigate this alert" and get a brief that cites incident IDs.

3/ Memory layer is @vectorize_io's Hindsight. The useful bit: it separates *what was true* from
*what we did*. So "priya fixed it by rolling back the retry endpoint" and "22 min wasted on an
unrelated node upgrade" are first-class memories, not paragraphs in a doc.

4/ The feature I'd defend hardest: it refuses. No genuine match in memory → it says "no precedent,
treat as novel." A confidently wrong citation at 3am is worse than no memory at all. Building the
refusal took longer than building the recall.

5/ And the "it learns" claim is measured, not asserted. Replay the whole incident history against an
empty memory, scoring each incident using only what existed before it. Coverage: 0% → 43%.
Precedent found when one existed: 9/9. No lookahead. Synthetic corpus, and I say so.

6/ Corpus numbers: first time a team sees a failure family, 108 min to resolve. A repeat, when
someone remembers, 44 min. STARK makes that drop happen on the first repeat — for whoever is
holding the pager, not whoever held it last time.

7/ Repo + demo: <link>

---

## Video outline (2 min)

| Time | Beat |
|---|---|
| 0:00 | The 3am framing — the responder is never the person who fixed it last time |
| 0:15 | Side-by-side: generic assistant vs memory-grounded brief on the same alert |
| 0:40 | Zoom the "do NOT do" list — false leads with incident citations. This is the money shot |
| 1:00 | The novel alert. It refuses to pattern-match. Explain why that matters more than recall |
| 1:20 | Teach it: record an outcome, re-run the same alert, watch novel become grounded |
| 1:40 | Learning tab: held-out replay, coverage curve, MTTR bars |
| 1:55 | Repo link |

Full beat-by-beat narration in `DEMO_SCRIPT.md`.
