"""
Memory layer for STARK.

STARK is built on **Hindsight** (https://hindsight.vectorize.io) and uses its
three core operations directly:

    retain()   store what happened during / after an incident
    recall()   multi-strategy search over everything the agent has ever seen
    reflect()  disposition-aware reasoning grounded in recalled memory

`HindsightBackend` is a thin wrapper over the official `hindsight-client`.

`LocalMemoryEngine` is a dependency-free fallback that implements the *same
interface* with the same conceptual model (world facts / experience facts /
observations / mental models, plus a 4-way keyword+graph+temporal+recency
retrieval blend). It exists so the demo boots with zero credentials and so the
Hindsight integration is testable offline. Set HINDSIGHT_BASE_URL and the app
transparently switches to the real thing — no other code changes.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------- #
# Shared types
# --------------------------------------------------------------------------- #

WORLD = "world"
EXPERIENCE = "experience"
OBSERVATION = "observation"
MENTAL_MODEL = "mental_model"

#: Priority order Hindsight uses during reflect: mental models beat observations,
#: observations beat raw facts.
TYPE_PRIORITY = {MENTAL_MODEL: 3.0, OBSERVATION: 2.0, EXPERIENCE: 1.2, WORLD: 1.0}


@dataclass
class MemoryHit:
    """One retrieved memory, normalised across backends."""

    text: str
    type: str = WORLD
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str | None = None
    strategies: list[str] = field(default_factory=list)
    proof_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MemoryItem:
    """One unit of content handed to retain()."""

    content: str
    context: str = ""
    type: str = WORLD
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str | None = None
    document_id: str | None = None


# --------------------------------------------------------------------------- #
# Tokenisation / entity extraction (shared by the local engine)
# --------------------------------------------------------------------------- #

_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for", "from",
    "had", "has", "have", "he", "her", "his", "i", "in", "is", "it", "its", "of",
    "on", "or", "our", "she", "that", "the", "their", "then", "there", "these",
    "they", "this", "to", "was", "we", "were", "what", "when", "which", "who",
    "will", "with", "you", "your", "it's", "we're", "into", "out", "up", "down",
}

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_\-./]*", re.I)

#: Things that behave like graph nodes in an SRE context.
_ENTITY_PATTERNS = [
    re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)*-(?:api|svc|service|worker|gateway|web|scorer|primary|replica|cache|queue|db)\b", re.I),
    re.compile(r"\b(?:pg|postgres|postgresql|redis|kafka|envoy|nginx|istio|rabbitmq|elasticsearch|clickhouse)\b", re.I),
    re.compile(r"\b(?:OOMKilled|CrashLoopBackOff|ECONNRESET|ETIMEDOUT|EPIPE|ENOSPC|SIGKILL|SIGTERM|deadlock|throttl\w*)\b", re.I),
    re.compile(r"\bHTTP\s?5\d{2}\b|\b5xx\b|\b4xx\b|\b\d{3}\s?(?:errors?)\b", re.I),
    re.compile(r"\bp(?:50|95|99|999)\b", re.I),
    re.compile(r"\b(?:connection[- ]pool|thread[- ]pool|circuit[- ]breaker|rate[- ]limit|autoscal\w+|failover|replica[- ]lag|disk|memory|cpu|latency|timeout|retry|backoff|migration|deploy(?:ment)?|rollback|feature[- ]flag)\b", re.I),
    re.compile(r"\b(?:jwks|jwt|kid|rotation|rotated|introspect|oauth|token|eviction|evicted|vacuum|schema|serialization|deserialization|poison|dead[- ]letter|dlq|certificate|x509|tls|bundle|invoice|otp|settlement|webhook|consumer[- ]lag|rebalanc\w+|hikari|pgbouncer|oomkilled)\b", re.I),
]

#: split CamelCase and letter/digit boundaries: "AuthGateway401Spike" -> "Auth Gateway 401 Spike"
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")


def normalize_text(text: str) -> str:
    """Split CamelCase alert names so 'AuthGateway401Spike' matches 'auth-gateway'."""
    spaced = _CAMEL_RE.sub(" ", text or "")
    return re.sub(r"[_/]+", " ", spaced)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(text or "") if t.lower() not in _STOP and len(t) > 1]


def extract_entities(text: str) -> set[str]:
    found: set[str] = set()
    haystack = (text or "") + " " + normalize_text(text or "").replace(" ", "-")
    for pattern in _ENTITY_PATTERNS:
        for match in pattern.findall(haystack):
            token = match if isinstance(match, str) else match[0]
            found.add(re.sub(r"\s+", "-", token.strip().lower()))
    return {e for e in found if e}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
# Local fallback engine
# --------------------------------------------------------------------------- #


@dataclass
class _Fact:
    id: int
    text: str
    type: str
    context: str
    metadata: dict[str, Any]
    timestamp: str
    document_id: str | None
    tokens: list[str] = field(default_factory=list)
    entities: set[str] = field(default_factory=set)


@dataclass
class _Observation:
    """A consolidated, evidence-grounded belief built from many facts."""

    key: str
    text: str
    proof_count: int
    evidence: list[str]
    entities: set[str]
    last_updated: str
    metadata: dict[str, Any] = field(default_factory=dict)


class LocalMemoryEngine:
    """
    A compact, Hindsight-shaped memory engine.

    Retrieval blends four signals, mirroring Hindsight's TEMPR:
      * keyword  — BM25 over the fact corpus
      * semantic — token-overlap proxy (no embedding model required offline)
      * graph    — entity co-occurrence expansion
      * temporal — recency decay + explicit time-range hints in the query
    """

    K1 = 1.5
    B = 0.75
    HALF_LIFE_DAYS = 120.0

    def __init__(self, state_path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._facts: list[_Fact] = []
        self._df: Counter[str] = Counter()
        self._observations: dict[str, _Observation] = {}
        self._mental_models: dict[str, dict[str, Any]] = {}
        self._entity_index: dict[str, set[int]] = defaultdict(set)
        self._next_id = 0
        self._state_path = state_path
        self._dirty_since_consolidation = 0
        if state_path and state_path.exists():
            self._load()

    # -- persistence -------------------------------------------------------- #

    def _load(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        for item in raw.get("facts", []):
            self._index_fact(
                _Fact(
                    id=item["id"],
                    text=item["text"],
                    type=item["type"],
                    context=item.get("context", ""),
                    metadata=item.get("metadata", {}),
                    timestamp=item.get("timestamp") or _now_iso(),
                    document_id=item.get("document_id"),
                )
            )
        self._mental_models = raw.get("mental_models", {})
        self._consolidate()

    def save(self) -> None:
        if not self._state_path:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "facts": [
                {
                    "id": f.id,
                    "text": f.text,
                    "type": f.type,
                    "context": f.context,
                    "metadata": f.metadata,
                    "timestamp": f.timestamp,
                    "document_id": f.document_id,
                }
                for f in self._facts
            ],
            "mental_models": self._mental_models,
        }
        self._state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def reset(self) -> None:
        with self._lock:
            self._facts.clear()
            self._df.clear()
            self._observations.clear()
            self._mental_models.clear()
            self._entity_index.clear()
            self._next_id = 0
            _clear_file(self._state_path)

    # -- retain ------------------------------------------------------------- #

    def _index_fact(self, fact: _Fact) -> None:
        fact.tokens = tokenize(fact.text + " " + fact.context)
        fact.entities = extract_entities(fact.text + " " + fact.context)
        for token in set(fact.tokens):
            self._df[token] += 1
        for entity in fact.entities:
            self._entity_index[entity].add(fact.id)
        self._facts.append(fact)
        self._next_id = max(self._next_id, fact.id + 1)

    def retain(self, items: Sequence[MemoryItem]) -> int:
        with self._lock:
            for item in items:
                for sentence in _split_facts(item.content):
                    self._index_fact(
                        _Fact(
                            id=self._next_id,
                            text=sentence,
                            type=item.type,
                            context=item.context,
                            metadata=dict(item.metadata),
                            timestamp=item.timestamp or _now_iso(),
                            document_id=item.document_id,
                        )
                    )
                    self._dirty_since_consolidation += 1
            self._consolidate()
            self.save()
            return len(self._facts)

    def set_mental_model(self, key: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._mental_models[key] = {
                "text": text,
                "metadata": metadata or {},
                "updated": _now_iso(),
            }
            self.save()

    # -- consolidation into observations ------------------------------------ #

    def _consolidate(self) -> None:
        """
        Merge overlapping facts into deduplicated, evidence-grounded observations.

        Grouping key = (service, failure_class). Every observation tracks how many
        distinct incidents support it and quotes its source facts, which is what
        makes the agent's advice auditable instead of hand-wavy.
        """
        groups: dict[str, list[_Fact]] = defaultdict(list)
        for fact in self._facts:
            service = fact.metadata.get("service")
            failure_class = fact.metadata.get("failure_class")
            if not service or not failure_class:
                continue
            groups[f"{service}::{failure_class}"].append(fact)

        observations: dict[str, _Observation] = {}
        for key, facts in groups.items():
            service, failure_class = key.split("::", 1)
            incidents = {f.metadata.get("incident_id") for f in facts if f.metadata.get("incident_id")}
            # Count DISTINCT incidents per fix, not facts. Each incident contributes
            # several facts that all carry the same `fix`, so counting facts would
            # claim more evidence than exists.
            fix_incidents: dict[str, set[str]] = defaultdict(set)
            mttr_by_incident: dict[str, float] = {}
            for fact in facts:
                iid = fact.metadata.get("incident_id")
                if fact.metadata.get("fix") and iid:
                    fix_incidents[fact.metadata["fix"]].add(iid)
                if iid and isinstance(fact.metadata.get("mttr_minutes"), (int, float)):
                    mttr_by_incident[iid] = fact.metadata["mttr_minutes"]
            fixes = Counter({fix: len(ids) for fix, ids in fix_incidents.items()})
            mttrs = list(mttr_by_incident.values())
            if len(incidents) < 2:
                continue

            top_fix, top_fix_n = (fixes.most_common(1) or [(None, 0)])[0]
            latest = max(facts, key=lambda f: _parse_ts(f.timestamp))
            latest_fix = latest.metadata.get("fix")
            parts = [
                f"{service} has hit '{failure_class}' in {len(incidents)} separate incidents."
            ]
            if top_fix and top_fix_n >= 2:
                parts.append(
                    f"The resolution that worked most often ({top_fix_n} of {len(incidents)}) was: "
                    f"{top_fix.rstrip('.')}."
                )
            elif latest_fix:
                # Every occurrence needed a different fix — that is itself the finding.
                parts.append(
                    f"No single resolution has repeated; each occurrence needed a different fix. "
                    f"The most recent one that worked was: {latest_fix.rstrip('.')}."
                )
                top_fix = latest_fix
            if mttrs:
                parts.append(f"Median time to resolve was {int(sorted(mttrs)[len(mttrs) // 2])} minutes.")
            observations[key] = _Observation(
                key=key,
                text=" ".join(parts),
                proof_count=len(incidents),
                evidence=[f.text for f in facts[:4]],
                entities=set().union(*[f.entities for f in facts]) if facts else set(),
                last_updated=max((f.timestamp for f in facts), default=_now_iso()),
                metadata={
                    "service": service,
                    "failure_class": failure_class,
                    "incident_ids": sorted(i for i in incidents if i),
                    "recommended_fix": top_fix,
                },
            )
        self._observations = observations
        self._dirty_since_consolidation = 0

    # -- recall ------------------------------------------------------------- #

    def _bm25(self, query_tokens: Sequence[str], fact: _Fact, avgdl: float) -> float:
        if not fact.tokens:
            return 0.0
        tf = Counter(fact.tokens)
        n_docs = max(len(self._facts), 1)
        score = 0.0
        for token in query_tokens:
            if token not in tf:
                continue
            df = self._df.get(token, 0) or 1
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            freq = tf[token]
            score += idf * (freq * (self.K1 + 1)) / (
                freq + self.K1 * (1 - self.B + self.B * len(fact.tokens) / max(avgdl, 1.0))
            )
        return score

    def recall(
        self,
        query: str,
        types: Sequence[str] | None = None,
        limit: int = 12,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[MemoryHit]:
        with self._lock:
            if not self._facts:
                return []

            query_tokens = tokenize(query)
            query_entities = extract_entities(query)
            avgdl = sum(len(f.tokens) for f in self._facts) / max(len(self._facts), 1)
            now = time.time()

            # graph expansion: entities that co-occur with the query's entities
            neighbours: set[str] = set(query_entities)
            for entity in query_entities:
                for fid in list(self._entity_index.get(entity, ()))[:80]:
                    neighbours |= self._facts[fid].entities

            scored: list[MemoryHit] = []
            for fact in self._facts:
                if metadata_filter and any(fact.metadata.get(k) != v for k, v in metadata_filter.items()):
                    continue

                strategies: list[str] = []
                keyword = self._bm25(query_tokens, fact, avgdl)
                if keyword > 0:
                    strategies.append("keyword")

                overlap = len(set(query_tokens) & set(fact.tokens))
                semantic = overlap / math.sqrt(len(set(query_tokens)) or 1)
                if semantic > 0:
                    strategies.append("semantic")

                direct = len(query_entities & fact.entities)
                indirect = len(neighbours & fact.entities) - direct
                graph = 2.0 * direct + 0.4 * max(indirect, 0)
                if graph > 0:
                    strategies.append("graph")

                age_days = max(now - _parse_ts(fact.timestamp), 0.0) / 86400.0
                temporal = 0.5 ** (age_days / self.HALF_LIFE_DAYS)
                if temporal > 0.5:
                    strategies.append("temporal")

                total = (1.0 * keyword + 1.2 * semantic + 1.0 * graph) * (0.6 + 0.4 * temporal)
                total *= TYPE_PRIORITY.get(fact.type, 1.0)
                if total <= 0:
                    continue
                scored.append(
                    MemoryHit(
                        text=fact.text,
                        type=fact.type,
                        score=round(total, 4),
                        metadata=fact.metadata,
                        timestamp=fact.timestamp,
                        strategies=strategies,
                    )
                )

            # observations + mental models compete at higher priority
            for obs in self._observations.values():
                direct = len(query_entities & obs.entities)
                overlap = len(set(query_tokens) & set(tokenize(obs.text)))
                if not direct and not overlap:
                    continue
                score = (2.0 * direct + 0.8 * overlap) * TYPE_PRIORITY[OBSERVATION]
                score *= 1 + 0.15 * obs.proof_count
                scored.append(
                    MemoryHit(
                        text=obs.text,
                        type=OBSERVATION,
                        score=round(score, 4),
                        metadata={**obs.metadata, "evidence": obs.evidence},
                        timestamp=obs.last_updated,
                        strategies=["consolidation", "graph"],
                        proof_count=obs.proof_count,
                    )
                )

            for key, model in self._mental_models.items():
                overlap = len(set(query_tokens) & set(tokenize(model["text"] + " " + key)))
                if overlap == 0:
                    continue
                scored.append(
                    MemoryHit(
                        text=model["text"],
                        type=MENTAL_MODEL,
                        score=round(overlap * TYPE_PRIORITY[MENTAL_MODEL], 4),
                        metadata={"key": key, **model.get("metadata", {})},
                        timestamp=model.get("updated"),
                        strategies=["curated"],
                    )
                )

            if types:
                allowed = set(types)
                scored = [h for h in scored if h.type in allowed]

            scored.sort(key=lambda h: h.score, reverse=True)
            return _dedupe_hits(scored)[:limit]

    # -- introspection ------------------------------------------------------ #

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by_type = Counter(f.type for f in self._facts)
            return {
                "facts": len(self._facts),
                "world_facts": by_type.get(WORLD, 0),
                "experience_facts": by_type.get(EXPERIENCE, 0),
                "observations": len(self._observations),
                "mental_models": len(self._mental_models),
                "entities": len(self._entity_index),
                "incidents": len({f.metadata.get("incident_id") for f in self._facts if f.metadata.get("incident_id")}),
            }

    def observations(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                (
                    {
                        "text": o.text,
                        "proof_count": o.proof_count,
                        "evidence": o.evidence,
                        **o.metadata,
                    }
                    for o in self._observations.values()
                ),
                key=lambda o: o["proof_count"],
                reverse=True,
            )


def _clear_file(path: Path | None) -> None:
    """Remove a state file, tolerating filesystems that forbid unlink (some
    mounted/synced folders do). Truncating is an acceptable equivalent."""
    if not path or not path.exists():
        return
    try:
        path.unlink()
    except OSError:
        try:
            path.write_text("{}", encoding="utf-8")
        except OSError:
            pass


def _split_facts(content: str) -> list[str]:
    """Break a blob into atomic, individually-retrievable facts."""
    chunks: list[str] = []
    for line in (content or "").splitlines():
        line = line.strip(" -•\t")
        if not line:
            continue
        if len(line) < 220:
            chunks.append(line)
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            sentence = sentence.strip()
            if sentence:
                chunks.append(sentence)
    return chunks or ([content.strip()] if content and content.strip() else [])


def _dedupe_hits(hits: Iterable[MemoryHit]) -> list[MemoryHit]:
    seen: set[str] = set()
    out: list[MemoryHit] = []
    for hit in hits:
        key = re.sub(r"\W+", "", hit.text.lower())[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


# --------------------------------------------------------------------------- #
# Hindsight backend
# --------------------------------------------------------------------------- #


class HindsightBackend:
    """Thin adapter over the official Hindsight client."""

    def __init__(self, base_url: str, bank_id: str, api_key: str = "") -> None:
        from hindsight_client import Hindsight  # imported lazily on purpose

        kwargs: dict[str, Any] = {"base_url": base_url, "timeout": 60.0}
        if api_key:
            kwargs["api_key"] = api_key
        self.client = Hindsight(**kwargs)
        self.bank_id = bank_id

    def ensure_bank(self, mission: str, directives: Sequence[str] = ()) -> None:
        try:
            self.client.create_bank(
                bank_id=self.bank_id,
                name="STARK On-Call",
                mission=mission,
                disposition={"skepticism": 4, "literalism": 4, "empathy": 2},
            )
        except Exception:
            pass  # bank already exists
        for directive in directives:
            try:
                self.client.directives.create(bank_id=self.bank_id, content=directive)
            except Exception:
                pass

    def retain(self, items: Sequence[MemoryItem]) -> int:
        payload = [
            {
                "content": item.content,
                "context": item.context,
                "metadata": item.metadata,
                **({"timestamp": item.timestamp} if item.timestamp else {}),
            }
            for item in items
        ]
        self.client.retain_batch(bank_id=self.bank_id, items=payload, retain_async=False)
        return len(payload)

    def recall(
        self,
        query: str,
        types: Sequence[str] | None = None,
        limit: int = 12,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[MemoryHit]:
        response = self.client.recall(
            bank_id=self.bank_id,
            query=query,
            types=list(types) if types else ["world", "experience", "observation"],
            budget="high",
            max_tokens=4096,
        )
        results = getattr(response, "results", response) or []
        hits: list[MemoryHit] = []
        for item in results[:limit]:
            hits.append(
                MemoryHit(
                    text=getattr(item, "text", str(item)),
                    type=getattr(item, "type", WORLD) or WORLD,
                    score=float(getattr(item, "score", 0.0) or 0.0),
                    metadata=dict(getattr(item, "metadata", {}) or {}),
                    timestamp=getattr(item, "timestamp", None),
                    strategies=["hindsight-tempr"],
                )
            )
        return hits

    def reflect(self, query: str, context: str = "") -> str | None:
        try:
            answer = self.client.reflect(
                bank_id=self.bank_id, query=query, context=context, budget="mid"
            )
            return getattr(answer, "text", None)
        except Exception:
            return None

    def stats(self) -> dict[str, Any]:
        try:
            memories = self.client.list_memories(bank_id=self.bank_id, limit=1000)
            items = getattr(memories, "memories", memories) or []
            by_type = Counter(getattr(m, "type", WORLD) for m in items)
            return {
                "facts": len(items),
                "world_facts": by_type.get(WORLD, 0),
                "experience_facts": by_type.get(EXPERIENCE, 0),
                "observations": by_type.get(OBSERVATION, 0),
                "mental_models": by_type.get(MENTAL_MODEL, 0),
                "entities": 0,
                "incidents": 0,
            }
        except Exception:
            return {"facts": 0, "observations": 0, "mental_models": 0, "incidents": 0}

    def observations(self) -> list[dict[str, Any]]:
        try:
            memories = self.client.list_memories(bank_id=self.bank_id, type="observation", limit=100)
            items = getattr(memories, "memories", memories) or []
            return [
                {
                    "text": getattr(m, "text", str(m)),
                    "proof_count": getattr(m, "proof_count", 1),
                    "evidence": [],
                    **dict(getattr(m, "metadata", {}) or {}),
                }
                for m in items
            ]
        except Exception:
            return []

    def reset(self) -> None:
        try:
            self.client.banks.delete(bank_id=self.bank_id)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #

BANK_MISSION = (
    "I am the on-call memory of a payments platform SRE team. I remember every "
    "incident this team has ever run: the alert that fired, the symptoms, the "
    "false leads, the true root cause, the fix that worked, and how long it took. "
    "When a new alert arrives my job is to surface the specific past incidents "
    "that resemble it and the resolution steps that actually worked, so the "
    "responder never re-derives a diagnosis the team has already paid for."
)

BANK_DIRECTIVES = (
    "Never invent an incident ID, a runbook, or a metric that is not present in memory.",
    "Always state how many past incidents support a recommendation.",
    "If memory contains no similar incident, say so plainly instead of guessing.",
    "Prefer the resolution with the best observed outcome, not the most recent one.",
)


class MemoryStore:
    """Picks the Hindsight backend when configured, local engine otherwise."""

    def __init__(self, settings) -> None:  # noqa: ANN001 - avoid circular import
        self.settings = settings
        self.mode = "local"
        self.backend: Any
        state_path = Path(settings.state_dir) / "memory.json"
        if settings.hindsight_enabled:
            try:
                backend = HindsightBackend(
                    base_url=settings.hindsight_base_url,
                    bank_id=settings.bank_id,
                    api_key=settings.hindsight_api_key,
                )
                backend.ensure_bank(BANK_MISSION, BANK_DIRECTIVES)
                self.backend = backend
                self.mode = "hindsight"
            except Exception as exc:  # pragma: no cover - network dependent
                print(f"[stark] Hindsight unavailable ({exc}); using local memory engine.")
                self.backend = LocalMemoryEngine(state_path)
        else:
            self.backend = LocalMemoryEngine(state_path)

    # Uniform surface -------------------------------------------------------- #

    def retain(self, items: Sequence[MemoryItem]) -> int:
        return self.backend.retain(items)

    def recall(self, query: str, **kwargs: Any) -> list[MemoryHit]:
        return self.backend.recall(query, **kwargs)

    def reflect(self, query: str, context: str = "") -> str | None:
        reflect = getattr(self.backend, "reflect", None)
        return reflect(query, context) if reflect else None

    def stats(self) -> dict[str, Any]:
        return {**self.backend.stats(), "mode": self.mode, "bank_id": self.settings.bank_id}

    def observations(self) -> list[dict[str, Any]]:
        return self.backend.observations()

    def set_mental_model(self, key: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        setter = getattr(self.backend, "set_mental_model", None)
        if setter:
            setter(key, text, metadata)
        else:  # Hindsight
            try:
                self.backend.client.mental_models.create(
                    bank_id=self.settings.bank_id, title=key, content=text
                )
            except Exception:
                pass

    def reset(self) -> None:
        self.backend.reset()
