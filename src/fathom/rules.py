"""Alert rules.

Each rule is a query that returns the things currently breaching, plus a
``for_cycles`` duration. A breach becomes an alert only after it has held for
that many consecutive polls — the same pending → firing model as Prometheus.
A single 90% utilisation sample is a burst; three in a row is congestion.

Alerts are keyed by a fingerprint (rule + device + target). While a
fingerprint is firing, repeated breaches update nothing; when the condition
clears, the alert is resolved and the fingerprint is free again. That gives
deduplication without any timers.

``interface_down`` deliberately covers only links in the discovered topology.
A user unplugging a laptop takes an access port down hundreds of times a day;
alerting on that would bury the one uplink that matters.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .store import Store


@dataclass(frozen=True)
class Breach:
    device: str
    target: str
    summary: str


@dataclass
class Rule:
    name: str
    severity: str
    for_cycles: int
    query: Callable[[Store], list[Breach]]
    description: str


def _unreachable(s: Store) -> list[Breach]:
    return [Breach(r["name"], "", f"{r['name']} ({r['mgmt']}) is not responding")
            for r in s.rows("SELECT name, mgmt FROM devices WHERE reachable = 0")]


def _link_down(s: Store) -> list[Breach]:
    # Only report the side we can still see: an unreachable device's last
    # known interface state is stale and would contradict its own alert.
    rows = s.rows("""
        SELECT i.device, i.name, l.b, l.b_if FROM interfaces i
        JOIN links l ON l.a = i.device AND l.a_if = i.name
        JOIN devices d ON d.name = i.device
        WHERE i.oper_up = 0 AND i.admin_up = 1 AND d.reachable = 1""")
    return [Breach(r["device"], r["name"], f"{r['device']} {r['name']} to {r['b']} {r['b_if']} is down") for r in rows]


def _errors(threshold: float) -> Callable[[Store], list[Breach]]:
    def q(s: Store) -> list[Breach]:
        rows = s.rows("""SELECT i.device, i.name, i.err_per_min FROM interfaces i JOIN devices d ON d.name=i.device
                         WHERE d.reachable=1 AND i.oper_up=1 AND i.err_per_min > ?""", (threshold,))
        return [Breach(r["device"], r["name"], f"{r['device']} {r['name']}: {r['err_per_min']:.0f} input errors/min")
                for r in rows]
    return q


def _util(threshold: float) -> Callable[[Store], list[Breach]]:
    def q(s: Store) -> list[Breach]:
        rows = s.rows("""SELECT i.device, i.name, i.util FROM interfaces i JOIN devices d ON d.name=i.device
                         WHERE d.reachable=1 AND i.oper_up=1 AND i.util > ?""", (threshold,))
        return [Breach(r["device"], r["name"], f"{r['device']} {r['name']} at {r['util']:.0f}% utilisation") for r in rows]
    return q


def _cpu(threshold: float) -> Callable[[Store], list[Breach]]:
    def q(s: Store) -> list[Breach]:
        return [Breach(r["name"], "", f"{r['name']} CPU at {r['cpu']:.0f}%")
                for r in s.rows("SELECT name, cpu FROM devices WHERE reachable=1 AND cpu > ?", (threshold,))]
    return q


def _err_disabled(s: Store) -> list[Breach]:
    # IOS shows err-disable in the interface state; VRP only says so in a log
    # line, so recent BPDU-protection events count too.
    rows = s.rows("""SELECT DISTINCT e.device, i.name FROM events e
                     JOIN interfaces i ON i.device = e.device
                          -- delimited match: Gi1/0/4 must not match a message about Gi1/0/48
                          AND (e.message LIKE '% ' || i.name || ',%' OR e.message LIKE '% ' || i.name || ' %')
                     WHERE (e.message LIKE '%err-disable%' OR e.message LIKE '%BPDU-Protection%')
                       AND i.oper_up = 0 AND i.admin_up = 1""")
    return [Breach(r["device"], r["name"], f"{r['device']} {r['name']} err-disabled by BPDU guard") for r in rows]


def default_rules() -> list[Rule]:
    return [
        Rule("device_unreachable", "critical", 1, _unreachable, "Device does not answer the poller."),
        Rule("link_down", "major", 1, _link_down, "An infrastructure link is down."),
        Rule("port_err_disabled", "minor", 1, _err_disabled, "A port was shut by a protection feature."),
        Rule("interface_errors", "warning", 2, _errors(30.0), "Sustained input/CRC errors, usually cabling or optics."),
        Rule("high_utilisation", "warning", 3, _util(80.0), "Sustained link utilisation above 80%."),
        Rule("high_cpu", "major", 2, _cpu(80.0), "Sustained control-plane CPU above 80%."),
    ]


class RuleEngine:
    def __init__(self, store: Store, rules: list[Rule] | None = None, clock=time.time):
        self.store = store
        self.rules = rules or default_rules()
        self.clock = clock
        self._pending: dict[str, int] = {}

    def evaluate(self) -> dict[str, list[int]]:
        now = self.clock()
        opened, resolved = [], []
        active: set[str] = set()
        for rule in self.rules:
            for b in rule.query(self.store):
                fp = f"{rule.name}|{b.device}|{b.target}"
                active.add(fp)
                self._pending[fp] = self._pending.get(fp, 0) + 1
                if self._pending[fp] < rule.for_cycles:
                    continue
                if self.store.row("SELECT id FROM alerts WHERE fingerprint=? AND status='firing'", (fp,)):
                    continue
                cur = self.store.execute(
                    "INSERT INTO alerts (fingerprint, rule, severity, device, target, summary, status, started) "
                    "VALUES (?,?,?,?,?,?,'firing',?)", (fp, rule.name, rule.severity, b.device, b.target, b.summary, now))
                opened.append(cur.lastrowid)

        for fp in list(self._pending):
            if fp not in active:
                del self._pending[fp]
        for a in self.store.rows("SELECT id, fingerprint FROM alerts WHERE status='firing'"):
            if a["fingerprint"] not in active:
                self.store.execute("UPDATE alerts SET status='resolved', ended=? WHERE id=?", (now, a["id"]))
                resolved.append(a["id"])
        return {"opened": opened, "resolved": resolved}
