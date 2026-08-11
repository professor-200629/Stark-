"""
The memory graph — what STARK knows, and how the pieces connect.

The list views elsewhere answer "what did you recall?". This answers the harder
question a judge or an on-call engineer actually asks: **why do you believe
that?** — by showing the path from a service, through a failure family, to the
specific incidents, root causes, fixes, dead ends and human decisions that
support a recommendation.

Two views:

  build_graph()      structural. Everything memory holds about a service or a
                     failure family, laid out as nodes and edges.
  graph_for_alert()  situational. Only the memories that a live alert actually
                     lit up, so the picture matches the brief on screen.

Layout is computed server-side and is deliberately deterministic — layered
columns rather than a force simulation — because a graph that rearranges itself
every time you open it is useless for a demo and impossible to point at.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .seed_data import INCIDENTS

#: Column each node type occupies, left to right: cause -> effect -> resolution.
COLUMNS = {
    "service": 0,
    "family": 1,
    "incident": 2,
    "cause": 3,
    "fix": 3,
    "false_lead": 3,
    "decision": 4,
}

NODE_LABELS = {
    "service": "Service",
    "family": "Failure family",
    "incident": "Incident",
    "cause": "Root cause",
    "fix": "Fix that worked",
    "false_lead": "Dead end",
    "decision": "Human decision",
}


def _node(
    node_id: str, kind: str, label: str, detail: str = "", **meta: Any
) -> dict[str, Any]:
    return {
        "id": node_id,
        "kind": kind,
        "label": label,
        "detail": detail,
        "column": COLUMNS[kind],
        **meta,
    }


def _truncate(text: str, limit: int = 88) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return cut[: cut.rfind(" ")] + "…" if " " in cut else cut + "…"


def build_graph(
    *,
    service: str = "",
    failure_class: str = "",
    incident_id: str = "",
    decisions: Any = None,
    max_incidents: int = 6,
    detail: bool = True,
) -> dict[str, Any]:
    """
    Build the structural graph for a service, a failure family, or one incident.

    With no filter it returns the service/family skeleton for the whole corpus —
    useful as an overview, but the interesting views are always filtered.
    """
    selected = [
        i
        for i in INCIDENTS
        if (not service or i["service"] == service)
        and (not failure_class or i["failure_class"] == failure_class)
        and (not incident_id or i["incident_id"] == incident_id)
    ]
    selected.sort(key=lambda i: i["opened_at"])
    if not selected:
        return {"nodes": [], "edges": [], "focus": {}, "empty": True}

    # Unfiltered view: skeleton only, or the picture becomes unreadable.
    skeleton = not (service or failure_class or incident_id)
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def add(node: dict[str, Any]) -> str:
        nodes.setdefault(node["id"], node)
        return node["id"]

    def link(source: str, target: str, kind: str) -> None:
        edge = {"source": source, "target": target, "kind": kind}
        if edge not in edges:
            edges.append(edge)

    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for incident in selected:
        by_family[incident["failure_class"]].append(incident)

    for family, incidents in by_family.items():
        services = sorted({i["service"] for i in incidents})
        mttrs = sorted(i["mttr_minutes"] for i in incidents)
        family_id = add(
            _node(
                f"fam:{family}",
                "family",
                family,
                f"{len(incidents)} incident(s), median {mttrs[len(mttrs) // 2]} min",
                occurrences=len(incidents),
            )
        )
        for svc in services:
            service_id = add(_node(f"svc:{svc}", "service", svc, "service"))
            link(service_id, family_id, "has hit")

        if skeleton:
            continue

        for incident in incidents[-max_incidents:]:
            iid = incident["incident_id"]
            incident_node = add(
                _node(
                    f"inc:{iid}",
                    "incident",
                    iid,
                    f"{incident['opened_at'][:10]} · {incident['severity']} · "
                    f"{incident['mttr_minutes']} min · {incident['responder']}",
                    opened_at=incident["opened_at"],
                    mttr_minutes=incident["mttr_minutes"],
                    alert_title=incident["alert_title"],
                )
            )
            link(family_id, incident_node, "occurred as")
            if not detail:
                continue

            link(
                incident_node,
                add(_node(f"cause:{iid}", "cause", _truncate(incident["root_cause"]),
                          incident["root_cause"], incident_id=iid)),
                "caused by",
            )
            link(
                incident_node,
                add(_node(f"fix:{iid}", "fix", _truncate(incident["fix"]),
                          incident["fix"], incident_id=iid)),
                "resolved by",
            )
            lead = incident.get("false_leads", "")
            if lead and not lead.lower().startswith("none"):
                link(
                    incident_node,
                    add(_node(f"lead:{iid}", "false_lead", _truncate(lead), lead, incident_id=iid)),
                    "wasted time on",
                )

    # Human decisions hang off the family they were made about.
    if decisions is not None and not skeleton:
        for record in getattr(decisions, "decisions", []):
            if failure_class and record.failure_class != failure_class:
                continue
            if service and record.service != service:
                continue
            family_id = f"fam:{record.failure_class}"
            if family_id not in nodes:
                continue
            outcome = (
                " · confirmed to work"
                if record.outcome == "worked"
                else " · did not work"
                if record.outcome == "did_not_work"
                else ""
            )
            node_id = add(
                _node(
                    f"dec:{record.recommendation_key}:{record.decision}",
                    "decision",
                    f"{record.decision} — {_truncate(record.action, 56)}",
                    f"{record.decided_by} {record.decision}d this{outcome}."
                    + (f" Reason: {record.note}" if record.note else ""),
                    decision=record.decision,
                    outcome=record.outcome,
                )
            )
            link(family_id, node_id, "team decided")

    return {
        "nodes": _layout(list(nodes.values())),
        "edges": edges,
        "focus": {"service": service, "failure_class": failure_class, "incident_id": incident_id},
        "skeleton": skeleton,
        "legend": NODE_LABELS,
        "empty": False,
    }


def _layout(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Assign deterministic x/y so the same graph is drawn identically every time.

    A force layout would look livelier and be useless on stage — you cannot point
    at a node that moved since the last render.
    """
    by_column: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for node in sorted(nodes, key=lambda n: (n["column"], n["id"])):
        by_column[node["column"]].append(node)

    tallest = max((len(v) for v in by_column.values()), default=1)
    row_height = 54
    height = max(tallest * row_height, 200)

    for column, column_nodes in by_column.items():
        step = height / (len(column_nodes) + 1)
        for index, node in enumerate(column_nodes, start=1):
            node["x"] = 40 + column * 210
            node["y"] = round(step * index)
    return nodes


def graph_for_alert(assembled: dict[str, Any], decisions: Any = None) -> dict[str, Any]:
    """
    The situational view: only what this alert actually recalled.

    Nodes carry `recalled=True`, so the UI can render the same shapes as the
    structural graph while making it obvious that this subset is what drove the
    brief currently on screen.
    """
    incident_ids = [c["incident_id"] for c in assembled.get("incidents", [])[:5]]
    if not incident_ids:
        return {"nodes": [], "edges": [], "empty": True, "focus": {}}

    families = {
        c["meta"].get("failure_class")
        for c in assembled.get("incidents", [])[:5]
        if c["meta"].get("failure_class")
    }
    graph = {"nodes": [], "edges": [], "focus": {}, "empty": False, "legend": NODE_LABELS}
    merged_nodes: dict[str, dict[str, Any]] = {}
    merged_edges: list[dict[str, Any]] = []

    for family in families:
        part = build_graph(failure_class=family, decisions=decisions)
        for node in part["nodes"]:
            node["recalled"] = node["kind"] in {"service", "family"} or any(
                iid in node["id"] for iid in incident_ids
            )
            merged_nodes.setdefault(node["id"], node)
        for edge in part["edges"]:
            if edge not in merged_edges:
                merged_edges.append(edge)

    # Drop incident branches that this alert did not actually recall.
    keep = {
        node_id
        for node_id, node in merged_nodes.items()
        if node["kind"] in {"service", "family"} or node.get("recalled")
    }
    graph["nodes"] = _layout([n for nid, n in merged_nodes.items() if nid in keep])
    graph["edges"] = [e for e in merged_edges if e["source"] in keep and e["target"] in keep]
    graph["focus"] = {"recalled_incidents": incident_ids}
    return graph


def build_timeline(failure_class: str) -> dict[str, Any]:
    """
    One failure family, in order, with what memory gained at each step.

    This is the accumulation story told as data rather than as a claim: the first
    occurrence teaches the system a root cause and a fix, the second adds a
    variant, and by the third the family is well characterised.
    """
    family = [i for i in INCIDENTS if i["failure_class"] == failure_class]
    family.sort(key=lambda i: i["opened_at"])
    if not family:
        return {"failure_class": failure_class, "entries": [], "empty": True}

    entries: list[dict[str, Any]] = []
    known_causes: list[str] = []
    for index, incident in enumerate(family, start=1):
        cause = _truncate(incident["root_cause"], 120)
        is_new_cause = cause not in known_causes
        known_causes.append(cause)
        entries.append(
            {
                "n": index,
                "incident_id": incident["incident_id"],
                "date": incident["opened_at"][:10],
                "service": incident["service"],
                "severity": incident["severity"],
                "responder": incident["responder"],
                "mttr_minutes": incident["mttr_minutes"],
                "alert_title": incident["alert_title"],
                "root_cause": incident["root_cause"],
                "fix": incident["fix"],
                "false_lead": incident.get("false_leads", ""),
                "first_of_family": index == 1,
                "new_root_cause": is_new_cause,
                "memory_before": (index - 1),
                "what_memory_gained": (
                    "First of its kind — nothing to recall, everything to learn."
                    if index == 1
                    else f"A {index}{'nd' if index == 2 else 'rd' if index == 3 else 'th'} "
                    f"data point for this family"
                    + (", and a root cause the family had not shown before." if is_new_cause
                       else ", confirming a cause already seen.")
                ),
            }
        )

    mttrs = [e["mttr_minutes"] for e in entries]
    return {
        "failure_class": failure_class,
        "entries": entries,
        "occurrences": len(entries),
        "mttr_series": mttrs,
        "first_mttr": mttrs[0],
        "latest_mttr": mttrs[-1],
        "empty": False,
    }


def families() -> list[dict[str, Any]]:
    """Every failure family, most-repeated first — the menu for the timeline view."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for incident in INCIDENTS:
        grouped[incident["failure_class"]].append(incident)
    return sorted(
        (
            {
                "failure_class": family,
                "occurrences": len(items),
                "services": sorted({i["service"] for i in items}),
                "mttr_series": [i["mttr_minutes"] for i in sorted(items, key=lambda x: x["opened_at"])],
            }
            for family, items in grouped.items()
        ),
        key=lambda f: (-f["occurrences"], f["failure_class"]),
    )


def incident_dossier(store: Any, incident_id: str) -> dict[str, Any]:
    """
    Everything memory holds about one incident, grouped by kind.

    This is what backs the "why does STARK believe this?" control: rather than
    asserting that a recommendation is supported, show the exact retained facts.
    """
    # Filter on metadata rather than hoping the query ranks every fact of one
    # incident into the top N — a dossier that silently omits the fix is worse
    # than no dossier at all.
    hits = store.recall(
        f"{incident_id} root cause fix resolved false lead symptoms verification alert impact",
        limit=40,
        metadata_filter={"incident_id": incident_id},
    )
    grouped: dict[str, list[str]] = defaultdict(list)
    meta: dict[str, Any] = {}
    for hit in hits:
        if hit.metadata.get("incident_id") != incident_id:
            continue
        grouped[hit.metadata.get("kind", "note")].append(hit.text)
        for key in ("service", "failure_class", "severity", "opened_at", "mttr_minutes", "responder"):
            if hit.metadata.get(key) and key not in meta:
                meta[key] = hit.metadata[key]

    return {
        "incident_id": incident_id,
        "meta": meta,
        "facts": {k: v for k, v in grouped.items()},
        "fact_count": sum(len(v) for v in grouped.values()),
        "empty": not grouped,
    }
