# Hindsight Made "No Precedent" a Successful Response

The demo I keep coming back to is the one where STARK says it doesn't know.

An alert fires on `webhook-dispatcher` — TLS verification failing against merchant endpoints, 61% delivery success. STARK pulls 18 candidate memories out of the bank, puts every one of them through the grounding gate, and none pass. It returns:

```
No incident in memory resembles this alert. Treat it as novel.
```

Eighteen candidates retrieved, zero admitted. That is not a failure mode I tolerated. It is the behaviour I spent the most time building.

## The problem with forcing agents to answer

Retrieval systems return their best match. Ask for top-k and you get k results, always, whether or not anything in the index is related. Reasonable for search; bad for a system that briefs a tired engineer at 3am.

Consider what a confidently wrong citation costs. The agent says *"this matches INC-1099, resolved by adding a TTL to the Redis keys."* Specific, plausible, actionable, with an incident number attached. They will chase it. An agent with no memory would have said "check your dashboards" and wasted nobody's time.

So the design constraint I started from: **the agent must be able to return nothing, and that has to be a first-class outcome rather than an error path.**

## How STARK decides whether memory is trustworthy

Alerts are mostly boilerplate — thresholds, durations, dashboard links. Retrieving on raw alert text means retrieving on boilerplate, so STARK fingerprints first: service, alert name, log signatures, entities. Alertmanager payloads arrive by webhook and normalise into that same shape, keeping the log line because it carries the signature that actually discriminates:

```python
    text = _lines(
        f"ALERT: {name}" if name else "",
        f"service={service} severity={severity}" if service or severity else "",
        label_line,
        annotations.get("summary"),
        # Runbook log lines are usually stuffed into an annotation; keep them,
        # they carry the error signature the fingerprinter actually keys on.
        *[f"{k}: {v}" for k, v in annotations.items() if k not in {"summary", "description"}],
    )
```

The fingerprint drives recall. Then, before anything reaches the reasoning step, a gate decides whether what came back has any business influencing the answer:

```python
        assembled["incidents"] = [
            c
            for c in assembled["incidents"]
            if c["relevance"]["service_match"]
            or c["relevance"]["entity_overlap"] >= 2
            or c["relevance"]["token_overlap"] >= 0.09
        ]
        if not assembled["incidents"]:
            assembled["observations"] = []
            assembled["mental_models"] = []
            return False
```

Three signals, any one sufficient. If nothing clears the bar, memory is withheld *entirely* — observations and curated playbooks dropped too, not just the incident cards.

That last detail took a while to get right. My first version dropped the incidents but left the consolidated observations in context, and the model happily reasoned from them, producing a confident brief grounded in a failure family unrelated to the alert. Partial withholding is not withholding.

## How Hindsight stores and retrieves experience

I built on [Hindsight](https://github.com/vectorize-io/hindsight) because it models something a vector store does not: the difference between what was true and what we did about it. Its [documentation](https://hindsight.vectorize.io/) calls these world facts and experience facts, and Vectorize's writeup on [agent memory](https://vectorize.io/what-is-agent-memory) explains why that distinction matters more than it first appears.

An incident does not go into the bank as a document — a postmortem stored whole is only retrievable whole, and you don't want eight paragraphs about INC-1042, you want the fix. Each incident is decomposed into a small set of atomic memories across both types:

```python
    items = [
        item("root_cause", f"{iid} root cause ({family}) on {service}: {incident['root_cause']}"),
        item(
            "fix",
            f"{iid}: {incident['responder']} resolved this {family} incident on {service} in "
            f"{incident['mttr_minutes']} minutes by: {incident['fix']}",
            EXPERIENCE,
        ),
    ]
    if incident.get("false_leads") and not incident["false_leads"].lower().startswith("none"):
        items.append(
            item(
                "false_lead",
                f"{iid} false lead — time was wasted here, do not repeat it: {incident['false_leads']}",
                EXPERIENCE,
            )
        )
```

The fix and the false lead are experience facts — things this team *did*, weighted above objective system behaviour at recall. Every item carries `incident_id`, `service`, `failure_class` and `mttr_minutes` as metadata, which is what lets flat search hits be reassembled into incident cards.

Recall blends keyword, semantic, entity-graph and temporal signals. On top I added a second hop: once the top incidents are known, go back and ask for *their* root cause, fix and false lead. One hop tells you which incident resembles this; two tell you what to do about it.

Consolidation is where Hindsight earns its place. Repeated facts collapse into observations carrying a proof count and their evidence, so the agent says things nobody wrote:

> payments-api has hit 'db-connection-pool-exhaustion' in 3 separate incidents. No single resolution has repeated; each occurrence needed a different fix. The most recent one that worked was: raise default_pool_size to 80 and max_client_conn to 5000. Median time to resolve was 52 minutes.

That "no single resolution has repeated" branch was not my first draft. Originally it always reported the most common fix — and since every incident in the family had a distinct one, it reported "1 of 3" every time. Technically true, completely useless. The absence of a repeating fix *is* the finding.

Three curated playbooks go in as mental models, outranking raw facts during reasoning. The bank is created with a mission, a skeptical disposition, and directives including *"if memory contains no similar incident, say so plainly instead of guessing."* The gate enforces that structurally; stating it in the bank config keeps the two aligned.

## The novel-alert → teach → remember loop

Before, on the `webhook-dispatcher` TLS alert:

```
verdict : No incident in memory resembles this alert. Treat it as novel.
recalled: 18 memories, grounded = False
```

The responder resolves it and records what happened — root cause, fix, and critically the twenty minutes lost to merchant firewall rules before anyone diffed the base image. That writes eight memories through the same `retain` path the corpus uses.

The identical alert, run again:

```
verdict : This matches the merchant-tls-trust-failure family, which this team has
          resolved once on webhook-dispatcher.
cites   : ['INC-1161']  | estimated 58 min
do-not  : Twenty minutes were spent on merchant-side firewall rules before anyone
          checked the base image diff.
```

Same alert text, same model, same prompts. The only thing that changed is the bank.

## Human decisions becoming memory

Grounded briefs propose actions; a human approves, rejects or defers each one. That ruling is retained as an experience fact in the same bank:

> On 2026-08-11, priya rejected STARK's recommendation for payments-api (db-connection-pool-exhaustion): raise default_pool_size to 80. Reason given: PgBouncer is already at 80 in prod since INC-1131.

Next time a similar alert fires, the recall that surfaces past incidents surfaces past decisions too — same retrieval path, same object type. Confirmed successes get promoted; rejected actions are demoted but kept visible with the reason they fell.

One bug is worth recording. Recommendation keys were hashed from the action text. On the deterministic path that text comes verbatim from memory, so keys were stable. With a live LLM the model paraphrases every sentence — each run minted a fresh key, no track record accumulated, and the approval loop silently did nothing. Keys are now anchored to the source incident.

## The benchmark and what it actually proves

`GET /api/benchmark` scores every incident twice, memory off and on, with the incident under test hidden from the bank at recall time. Without leave-one-out the memory arm is reading the answer sheet.

The result I'd defend: **without memory the agent recommended something this team had already proved was a waste of time on 2 of 21 incidents. With memory, 0 of 21.** Both hits were *"check recent deploys and roll back if one correlates in time"*, matched against INC-1042 and INC-1131, whose postmortems record the team almost rolling back an innocent deploy. Textbook-correct advice this specific team had already paid to learn was wrong. Advice containing a real parameter or config key went from 0% to 67%.

The row I point at myself is the one memory loses. With a capable model the memoryless arm names the failure mechanism *more* often than the grounded one; `HikariPool-1 - Connection is not available` names its own cause. Memory does not make the model a better diagnostician, and the code says so in that row's own metadata rather than quietly omitting it.

## Limitations

The corpus is synthetic. I wrote it. The evaluation over it is real and reproducible, but it measures retrieval behaviour, not production outcomes — STARK has never run against a live incident and claims no MTTR reduction.

Within that synthetic corpus, first-time occurrences of a failure family have a median time to resolve of 72.5 minutes and repeats have 43 minutes — a 40.7% difference. That gap is a property of the data I authored, not something STARK produced. It describes the problem the project is aimed at; it is not evidence that the project solves it.

The clearest failure mode is over-grounding. In the chronological replay, 3 of 12 novel incidents were wrongly matched — all three to a different failure family on the *correct* service. The service-match signal does a lot of work, and for a one-off failure on a busy service it fires when it shouldn't. Tightening it trades against recall, and for on-call work recall is the more expensive side to lose.

## Lessons learned

Building the refusal took longer than building the recall, and I'd make that trade again. The gate is thirty lines; getting it to withhold *everything* rather than most things, and proving it with a held-out replay rather than a hand-picked alert, was the work.

Two things I'd tell anyone building on a memory layer. Decompose before you store — a document is only retrievable as a document. And record what didn't work: root causes make it into runbooks, wrong turns evaporate, and they are the only thing in a postmortem no model can infer from the alert in front of it.

The useful memory of an incident is not just what fixed it. It is what the team believed, what it tried, what it rejected, and what it learned afterward. Hindsight gave me a way to keep those distinctions instead of flattening every postmortem into another document to retrieve.

For STARK, "I don't have a precedent" is not an embarrassing answer. It is a safety boundary. And when the team does learn something new, the next incident doesn't start from zero.
