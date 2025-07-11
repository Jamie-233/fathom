"""Topology-aware root cause analysis.

When a distribution switch dies, an alert-per-symptom system pages for the
switch, both access switches behind it, and every link facing it — six
alerts for one fault, and the responder has to work out which one is the
cause. Here every firing alert is attributed to a single root, and alerts
sharing a root become one incident.

The method follows how an engineer reasons from the poller's point of view:

1. Walk the shortest path from the management anchors (where the poller
   sits) to each unreachable device, and stop at the **first broken
   element** — a link reported down by its visible end, or a device that
   doesn't answer. Everything beyond that point is a symptom.

2. Decide whether a dark device is itself dead or merely cut off. If every
   reachable neighbour sees its link to the device as down, the device is
   the common factor: a **device** fault. If only one link is down and the
   device has no other path, link and device failure look identical from
   outside — the platform says so, with lower confidence, instead of
   pretending to know.

3. Alerts that have no topological relationship (CPU, errors, utilisation)
   are their own root.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from fathom.data.store import Store


@dataclass
class Finding:
    key: str                       # stable identity of the root, e.g. "device:dist-01"
    kind: str                      # device | link | local
    device: str
    target: str
    title: str
    confidence: str
    alerts: list[int] = field(default_factory=list)
    root_alerts: list[int] = field(default_factory=list)
    impact: list[str] = field(default_factory=list)


class Topology:
    def __init__(self, store: Store):
        self.adj: dict[str, list[tuple[str, str, str]]] = {}
        for l in store.rows("SELECT a, a_if, b, b_if FROM links"):
            self._add(l["a"], l["a_if"], l["b"], l["b_if"])
            self._add(l["b"], l["b_if"], l["a"], l["a_if"])
        devices = store.rows("SELECT name, role, reachable FROM devices")
        self.reachable = {d["name"]: bool(d["reachable"]) for d in devices}
        self.anchors = [d["name"] for d in devices if d["role"] == "core" and d["reachable"]]
        self.iface_up = {(r["device"], r["name"]): bool(r["oper_up"])
                         for r in store.rows("SELECT device, name, oper_up FROM interfaces")}

    def _add(self, src: str, src_if: str, dst: str, dst_if: str) -> None:
        """Record one direction of a link, once.

        LLDP normally reports from both ends, so the reverse entry is usually
        already in the table and this is a no-op. When only one end answers —
        a device that was dark during discovery, a link that only ever
        reported one way — the synthesized reverse entry is what keeps the
        graph usable. Relying on symmetry silently lost those links, and with
        them the path search and the "is every neighbour down?" check.
        """
        bucket = self.adj.setdefault(src, [])
        for iface, nb, _ in bucket:
            if iface == src_if and nb == dst:
                return
        bucket.append((src_if, dst, dst_if))

    def link_down(self, a: str, a_if: str, b: str, b_if: str) -> bool | None:
        """Down if any *visible* end says so; unknown if neither end is visible."""
        views = [self.iface_up.get((d, i)) for d, i in ((a, a_if), (b, b_if)) if self.reachable.get(d)]
        views = [v for v in views if v is not None]
        if not views:
            return None
        return not all(views)

    def path(self, dst: str) -> list[tuple[str, str, str, str]]:
        """Shortest path from any anchor to dst over the full topology, as links."""
        if not self.anchors:
            return []
        prev: dict[str, tuple[str, str, str, str]] = {}
        seen = set(self.anchors)
        q = deque(self.anchors)
        while q:
            cur = q.popleft()
            if cur == dst:
                break
            for a_if, nb, b_if in self.adj.get(cur, []):
                if nb not in seen:
                    seen.add(nb)
                    prev[nb] = (cur, a_if, nb, b_if)
                    q.append(nb)
        if dst not in prev and dst not in self.anchors:
            return []
        hops = []
        node = dst
        while node in prev:
            hops.append(prev[node])
            node = prev[node][0]
        return list(reversed(hops))

    def device_dead(self, dev: str) -> bool:
        """All links seen from reachable neighbours are down."""
        seen = [self.link_down(dev, i, nb, ni) for i, nb, ni in self.adj.get(dev, []) if self.reachable.get(nb)]
        return bool(seen) and all(s for s in seen if s is not None)


def link_key(a: str, a_if: str, b: str, b_if: str) -> str:
    # Both ends of a link raise their own alert; a direction-free key makes
    # them land in the same finding instead of two incidents for one cable.
    (x, xi), (y, yi) = sorted([(a, a_if), (b, b_if)])
    return f"link:{x}:{xi}:{y}:{yi}"


def analyze(store: Store) -> list[Finding]:
    topo = Topology(store)
    alerts = store.rows("SELECT id, rule, device, target FROM alerts WHERE status='firing' ORDER BY id")
    findings: dict[str, Finding] = {}

    def finding(key: str, **kw) -> Finding:
        if key not in findings:
            findings[key] = Finding(key=key, **kw)
        return findings[key]

    def root_for(dev: str) -> Finding:
        for a, a_if, b, b_if in topo.path(dev):
            if not topo.reachable.get(b):
                if topo.device_dead(b) and len([n for _, n, _ in topo.adj.get(b, []) if topo.reachable.get(n)]) > 1:
                    return finding(f"device:{b}", kind="device", device=b, target="",
                                   title=f"{b} is down", confidence="high")
                if topo.link_down(a, a_if, b, b_if):
                    return finding(link_key(a, a_if, b, b_if), kind="link", device=a, target=a_if,
                                   title=f"{b} isolated: link {a} {a_if} ↔ {b} {b_if} is down "
                                         f"(link or {b} itself — indistinguishable from outside)",
                                   confidence="medium")
                return finding(f"device:{b}", kind="device", device=b, target="",
                               title=f"{b} is unreachable", confidence="low")
        return finding(f"device:{dev}", kind="device", device=dev, target="",
                       title=f"{dev} is unreachable", confidence="low")

    # Pass 1: unreachable devices define the structural roots.
    for a in alerts:
        if a["rule"] == "device_unreachable":
            f = root_for(a["device"])
            f.alerts.append(a["id"])
            if f.kind == "device" and f.device == a["device"]:
                f.root_alerts.append(a["id"])
            elif a["device"] not in f.impact:
                f.impact.append(a["device"])

    # Pass 2: link alerts either belong to a structural root or are one.
    for a in alerts:
        if a["rule"] != "link_down":
            continue
        nb, nb_if = next(((n, ni) for i, n, ni in topo.adj.get(a["device"], []) if i == a["target"]), (None, ""))
        dead_key = f"device:{nb}"
        lkey = link_key(a["device"], a["target"], nb or "", nb_if)
        if dead_key in findings:
            findings[dead_key].alerts.append(a["id"])            # a symptom of the dead neighbour
        elif lkey in findings:
            findings[lkey].alerts.append(a["id"])
            findings[lkey].root_alerts.append(a["id"])
        else:
            # A redundant link failed: nothing became unreachable, but
            # resilience is gone, which is worth an incident of its own.
            f = finding(lkey, kind="link", device=a["device"], target=a["target"],
                        title=f"Link {a['device']} {a['target']} ↔ {nb} is down (redundancy lost)",
                        confidence="high")
            f.alerts.append(a["id"])
            f.root_alerts.append(a["id"])

    # Pass 3: everything else is local to its device/port.
    for a in alerts:
        if a["rule"] in ("device_unreachable", "link_down"):
            continue
        key = f"local:{a['rule']}:{a['device']}:{a['target']}"
        f = finding(key, kind="local", device=a["device"], target=a["target"],
                    title=_local_title(a), confidence="high")
        f.alerts.append(a["id"])
        f.root_alerts.append(a["id"])

    return list(findings.values())


def _local_title(a: dict) -> str:
    where = f"{a['device']} {a['target']}".strip()
    return {
        "interface_errors": f"Errors on {where} (likely cabling or optics)",
        "high_utilisation": f"Congestion on {where}",
        "high_cpu": f"High CPU on {a['device']}",
        "port_err_disabled": f"{where} shut by BPDU guard",
    }.get(a["rule"], f"{a['rule']} on {where}")


class IncidentManager:
    """Keeps incidents in step with findings across evaluation cycles.

    Incidents are matched to findings by root key, stored on the incident,
    so a restart doesn't reopen everything as new. When a finding's root
    shifts — a link alert first, then the whole device is found dead — the
    old incident closes and a new one opens with the better explanation,
    rather than one incident's title silently changing meaning.
    """

    def __init__(self, store: Store, clock=time.time):
        self.store = store
        self.clock = clock

    def sync(self) -> dict[str, list[int]]:
        now = self.clock()
        opened, closed = [], []
        live = set()
        for f in analyze(self.store):
            live.add(f.key)
            row = self.store.row("SELECT id FROM incidents WHERE status='open' AND root_key=?", (f.key,))
            if row is None:
                cur = self.store.execute(
                    "INSERT INTO incidents (title, root_key, root_device, root_target, kind, confidence, status, opened, impact) "
                    "VALUES (?,?,?,?,?,?,'open',?,?)",
                    (f.title, f.key, f.device, f.target, f.kind, f.confidence, now, ",".join(f.impact)))
                inc_id = cur.lastrowid
                opened.append(inc_id)
            else:
                inc_id = row["id"]
                self.store.execute("UPDATE incidents SET title=?, confidence=?, impact=? WHERE id=?",
                                   (f.title, f.confidence, ",".join(f.impact), inc_id))
            for aid in f.alerts:
                self.store.execute("UPDATE alerts SET incident_id=?, root=? WHERE id=?",
                                   (inc_id, int(aid in f.root_alerts), aid))
        for inc in self.store.rows("SELECT id, root_key FROM incidents WHERE status='open'"):
            if inc["root_key"] not in live:
                self.store.execute("UPDATE incidents SET status='closed', closed=? WHERE id=?", (now, inc["id"]))
                closed.append(inc["id"])
        return {"opened": opened, "closed": closed}
