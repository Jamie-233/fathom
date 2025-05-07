"""Network state verification, in the style of pyATS snapshots.

Monitoring says what is true now; verification answers a narrower question
around a change: *did this change break anything that was working?* That
needs a snapshot before and after, taken from the devices themselves rather
than the monitoring database, so a stale poll can never make a broken change
look fine.

The comparison is deliberately asymmetric. Something that was already broken
before the change is reported but doesn't fail the verification — otherwise
one unrelated dead port would block every change in the network forever.
Only regressions (working before, broken after) fail it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from fathom.data.parsers import parse_ios_interfaces, parse_vrp_brief
from fathom.data.store import Store
from fathom.devices.lab import DeviceUnreachable
from fathom.observe.collector import COMMANDS

CRC_TOLERANCE = 50


@dataclass
class Snapshot:
    reachable: dict[str, bool] = field(default_factory=dict)
    ports: dict[tuple[str, str], dict] = field(default_factory=dict)
    links: list[tuple[str, str, str, str]] = field(default_factory=list)

    def link_up(self, a: str, ai: str, b: str, bi: str) -> bool | None:
        ends = [self.ports.get((a, ai)), self.ports.get((b, bi))]
        seen = [e for e in ends if e is not None]
        return None if not seen else all(e["oper_up"] for e in seen)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def snapshot(store: Store, transport) -> Snapshot:
    snap = Snapshot()
    seen = set()
    for l in store.rows("SELECT a, a_if, b, b_if FROM links"):
        key = tuple(sorted([(l["a"], l["a_if"]), (l["b"], l["b_if"])]))
        if key not in seen:
            seen.add(key)
            snap.links.append((key[0][0], key[0][1], key[1][0], key[1][1]))

    def grab(d: dict):
        try:
            outs = [transport.run(d["name"], c) for c in COMMANDS[d["vendor"]]["interfaces"]]
        except DeviceUnreachable:
            return d["name"], None
        stats = parse_ios_interfaces(outs[0]) if d["vendor"] == "cisco_ios" else parse_vrp_brief(*outs)
        return d["name"], stats

    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, stats in pool.map(grab, store.rows("SELECT name, vendor FROM devices")):
            snap.reachable[name] = stats is not None
            for s in stats or []:
                snap.ports[(name, s.name)] = {"oper_up": s.oper_up, "admin_up": s.admin_up,
                                              "crc": s.crc, "err_disabled": s.err_disabled}
    return snap


def checks(s: Snapshot) -> list[Check]:
    """Absolute health checks on one snapshot."""
    dark = sorted(d for d, ok in s.reachable.items() if not ok)
    down = [f"{a} {ai}↔{b} {bi}" for a, ai, b, bi in s.links if s.link_up(a, ai, b, bi) is False]
    errdis = sorted(f"{d} {i}" for (d, i), p in s.ports.items() if p["err_disabled"])
    return [
        Check("all_devices_reachable", not dark, "all reachable" if not dark else f"unreachable: {', '.join(dark)}"),
        Check("all_links_up", not down, "all links up" if not down else f"down: {'; '.join(down)}"),
        Check("no_err_disabled_ports", not errdis, "none" if not errdis else ", ".join(errdis)),
    ]


def compare(before: Snapshot, after: Snapshot) -> list[Check]:
    """Regressions introduced between two snapshots."""
    lost = sorted(d for d, ok in before.reachable.items() if ok and not after.reachable.get(d, False))
    links = [f"{a} {ai}↔{b} {bi}" for a, ai, b, bi in before.links
             if before.link_up(a, ai, b, bi) and after.link_up(a, ai, b, bi) is False]
    ports = sorted(f"{d} {i}" for (d, i), p in before.ports.items()
                   if p["oper_up"] and (d, i) in after.ports and not after.ports[(d, i)]["oper_up"]
                   and after.ports[(d, i)]["admin_up"])
    crc = sorted(f"{d} {i} +{after.ports[(d, i)]['crc'] - p['crc']}" for (d, i), p in before.ports.items()
                 if (d, i) in after.ports and after.ports[(d, i)]["crc"] - p["crc"] > CRC_TOLERANCE)
    return [
        Check("no_devices_lost", not lost, "none" if not lost else ", ".join(lost)),
        Check("no_links_lost", not links, "none" if not links else "; ".join(links)),
        # A port someone deliberately shut is a change, not a regression.
        Check("no_ports_unexpectedly_down", not ports, "none" if not ports else ", ".join(ports)),
        Check("no_crc_growth", not crc, "none" if not crc else ", ".join(crc)),
    ]
