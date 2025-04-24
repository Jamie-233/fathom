"""Polling and rate calculation.

Devices expose monotonically increasing counters, not rates. A rate needs
two samples, and three things go wrong in the subtraction:

* **Wrap.** A 64-bit counter wraps to zero; a naive delta becomes hugely
  negative. If the counter went down by more than half its range, it wrapped.
* **Reset.** A reboot or ``clear counters`` also makes the value go down, but
  by a small amount. Treating that as a wrap would invent ~2^64 bytes of
  traffic, so a small decrease means "start over", not "wrapped".
* **Gaps.** A device that was unreachable for ten minutes produces one delta
  spanning ten minutes; dividing by the real elapsed time keeps the average
  honest instead of reporting a spike.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from fathom.devices.lab import DeviceUnreachable
from fathom.data.parsers import InterfaceStat, LogEvent, parse_cpu, parse_ios_interfaces, parse_lldp, \
    parse_syslog, parse_uptime_seconds, parse_vrp_brief
from fathom.data.store import Store

COUNTER_MAX = 2**64

COMMANDS = {
    "cisco_ios": {"interfaces": ("show interfaces",), "cpu": "show processes cpu",
                  "version": "show version", "lldp": "show lldp neighbors"},
    "huawei_vrp": {"interfaces": ("display interface brief", "display interface counters"),
                   "cpu": "display cpu-usage", "version": "display version", "lldp": "display lldp neighbor brief"},
}


def counter_delta(prev: int, cur: int, bits: int = 64) -> int | None:
    """Delta between two counter readings, or None when it was reset."""
    if cur >= prev:
        return cur - prev
    span = 2**bits
    if prev - cur > span // 2:
        return cur + span - prev
    return None


@dataclass
class _Last:
    ts: float
    in_bytes: int | None
    out_bytes: int | None
    errors: int


@dataclass
class PollResult:
    device: str
    reachable: bool
    interfaces: list[InterfaceStat] = field(default_factory=list)
    events: list[LogEvent] = field(default_factory=list)
    error: str = ""


class Collector:
    def __init__(self, store: Store, transport, clock=time.time):
        self.store = store
        self.transport = transport
        self.clock = clock
        self._last: dict[tuple[str, str], _Last] = {}

    def register(self, devices: list[dict]) -> None:
        for d in devices:
            self.store.execute(
                "INSERT INTO devices (name, vendor, role, mgmt, model) VALUES (?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET vendor=excluded.vendor, role=excluded.role, "
                "mgmt=excluded.mgmt, model=excluded.model",
                (d["name"], d["vendor"], d["role"], d["mgmt"], d["model"]),
            )

    def discover(self) -> int:
        """Build the topology from LLDP rather than trusting a hand-kept diagram."""
        now = self.clock()
        found = 0
        for d in self.store.rows("SELECT name, vendor FROM devices"):
            try:
                text = self.transport.run(d["name"], COMMANDS[d["vendor"]]["lldp"])
            except DeviceUnreachable:
                continue
            for local, peer, peer_if in parse_lldp(text):
                self.store.execute("INSERT OR REPLACE INTO links VALUES (?,?,?,?,?)",
                                   (d["name"], local, peer, peer_if, now))
                found += 1
        return found

    def poll(self, device: dict) -> PollResult:
        name, vendor = device["name"], device["vendor"]
        cmds = COMMANDS[vendor]
        now = self.clock()
        try:
            outputs = [self.transport.run(name, c) for c in cmds["interfaces"]]
            cpu = parse_cpu(self.transport.run(name, cmds["cpu"]))
            uptime = parse_uptime_seconds(self.transport.run(name, cmds["version"]))
            logs = self.transport.syslog(name)
        except DeviceUnreachable as exc:
            self.store.execute("UPDATE devices SET reachable=0 WHERE name=?", (name,))
            self.store.execute("INSERT INTO metrics VALUES (?,?,?,?,?)", (now, name, "reachable", "", 0))
            return PollResult(name, False, error=str(exc))

        stats = parse_ios_interfaces(outputs[0]) if vendor == "cisco_ios" else parse_vrp_brief(*outputs)
        events = [e for e in (parse_syslog(name, line) for line in logs) if e]

        with self.store.transaction() as s:
            s.db.execute("UPDATE devices SET reachable=1, cpu=?, uptime=?, last_seen=? WHERE name=?",
                         (cpu, uptime, now, name))
            s.db.executemany("INSERT INTO metrics VALUES (?,?,?,?,?)",
                             [(now, name, "reachable", "", 1), (now, name, "cpu", "", cpu)])
            for st in stats:
                self._write_interface(s, name, st, now)
            for e in events:
                s.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                             (now, name, e.severity, e.kind, e.interface, e.state, e.message))
        return PollResult(name, True, stats, events)

    def _write_interface(self, s: Store, device: str, st: InterfaceStat, now: float) -> None:
        key = (device, st.name)
        prev = self._last.get(key)
        in_bps = out_bps = err_rate = None
        if prev and st.in_bytes is not None and prev.in_bytes is not None:
            elapsed = now - prev.ts
            if elapsed > 0:
                d_in = counter_delta(prev.in_bytes, st.in_bytes)
                d_out = counter_delta(prev.out_bytes or 0, st.out_bytes or 0)
                d_err = st.in_errors - prev.errors if st.in_errors >= prev.errors else None
                if d_in is not None and d_out is not None:
                    in_bps, out_bps = d_in * 8 / elapsed, d_out * 8 / elapsed
                if d_err is not None:
                    err_rate = d_err / elapsed * 60
        self._last[key] = _Last(now, st.in_bytes, st.out_bytes, st.in_errors)

        speed = st.speed_mbps or (10_000 if st.name.startswith(("Te", "XGE")) else 1_000)
        util = None if in_bps is None else round(max(in_bps, out_bps or 0) / (speed * 1e6) * 100, 2)
        s.db.execute(
            "INSERT INTO interfaces VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(device, name) DO UPDATE SET "
            "admin_up=excluded.admin_up, oper_up=excluded.oper_up, description=excluded.description, "
            "speed_mbps=excluded.speed_mbps, in_bps=COALESCE(excluded.in_bps, interfaces.in_bps), "
            "out_bps=COALESCE(excluded.out_bps, interfaces.out_bps), util=COALESCE(excluded.util, interfaces.util), "
            "err_per_min=COALESCE(excluded.err_per_min, interfaces.err_per_min), crc_total=excluded.crc_total, "
            "updated=excluded.updated",
            (device, st.name, int(st.admin_up), int(st.oper_up), st.description, speed,
             in_bps, out_bps, util, err_rate, st.crc, now),
        )
        rows = [(now, device, "oper_up", st.name, int(st.oper_up))]
        if util is not None:
            rows += [(now, device, "util", st.name, util), (now, device, "in_bps", st.name, in_bps),
                     (now, device, "out_bps", st.name, out_bps)]
        if err_rate is not None:
            rows.append((now, device, "err_per_min", st.name, err_rate))
        s.db.executemany("INSERT INTO metrics VALUES (?,?,?,?,?)", rows)

    def poll_all(self) -> list[PollResult]:
        return [self.poll(d) for d in self.store.rows("SELECT name, vendor FROM devices ORDER BY name")]
