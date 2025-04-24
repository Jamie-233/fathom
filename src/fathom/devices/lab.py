"""A simulated network that behaves like real devices from the outside.

The platform never touches simulator internals. It talks to the lab the way
it would talk to hardware — by sending CLI commands and parsing the text
that comes back, and by receiving syslog lines. That keeps the rest of the
code honest: swap :class:`LabTransport` for an SSH transport and nothing
upstream changes.

Faults are modelled on what actually breaks networks: a link going down
(both ends see it, and everything behind it becomes unreachable), a port
accumulating CRC errors from a bad cable, a CPU pinned by a control-plane
storm, a whole device losing power. Each fault produces the same evidence a
real one would — counters, interface state, log messages — and nothing more.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_TOPOLOGY = Path(__file__).parent / "lab.yml"
COUNTER_MAX = 2**64


@dataclass
class SimInterface:
    name: str
    speed_mbps: int
    description: str = ""
    admin_up: bool = True
    link_up: bool = True
    in_octets: int = 0
    out_octets: int = 0
    in_errors: int = 0
    crc: int = 0
    base_load: float = 0.1        # fraction of line rate
    error_rate: float = 0.0       # errors per second when faulty
    err_disabled: bool = False
    peer: tuple[str, str] | None = None

    @property
    def oper_up(self) -> bool:
        return self.admin_up and self.link_up and not self.err_disabled


@dataclass
class SimDevice:
    name: str
    vendor: str
    role: str
    mgmt: str
    model: str
    interfaces: dict[str, SimInterface] = field(default_factory=dict)
    powered: bool = True
    cpu: float = 8.0
    cpu_fault: bool = False
    uptime: float = 0.0
    hostname_cfg: str = ""
    syslog: list[str] = field(default_factory=list)


class DeviceUnreachable(ConnectionError):
    pass


# The poller sits behind the core, so management reachability is a graph
# question, not a power question: an access switch with a cut uplink is
# alive and forwarding locally, yet the platform cannot see it at all. That
# is exactly why "device unreachable" alone can't name a root cause.
MGMT_ANCHORS = ("core-01", "core-02")


class Lab:
    def __init__(self, topology: str | Path = DEFAULT_TOPOLOGY, seed: int | None = 7):
        spec = yaml.safe_load(Path(topology).read_text(encoding="utf-8"))
        self.site = spec.get("site", "lab")
        self.rng = random.Random(seed)
        self.devices: dict[str, SimDevice] = {}
        self.links: list[tuple[str, str, str, str]] = []
        self.clock = time.time()
        # One lock for the whole lab: the API, the poller and the tick thread
        # all touch it, and a torn read (counter from before a tick, state
        # from after) would show up as a phantom traffic spike.
        self.lock = threading.RLock()

        for d in spec["devices"]:
            self.devices[d["name"]] = SimDevice(d["name"], d["vendor"], d["role"], d["mgmt"], d["model"],
                                                uptime=self.rng.uniform(5, 90) * 86400)
        for a, ai, b, bi in spec["links"]:
            speed = 10_000 if ai.startswith(("Te", "XGE")) or bi.startswith(("Te", "XGE")) else 1_000
            self._iface(a, ai, speed, f"to-{b}", load=self.rng.uniform(0.15, 0.4)).peer = (b, bi)
            self._iface(b, bi, speed, f"to-{a}", load=self.rng.uniform(0.15, 0.4)).peer = (a, ai)
            self.links.append((a, ai, b, bi))

        for d in self.devices.values():
            if d.role == "access":
                prefix = "Gi1/0/" if d.vendor == "cisco_ios" else "GE0/0/"
                for i in range(1, spec.get("access_ports", 6) + 1):
                    self._iface(d.name, f"{prefix}{i}", 1000, f"user-port-{i}", load=self.rng.uniform(0.01, 0.2))

        for d in self.devices.values():
            for i in d.interfaces.values():
                i.in_octets = self.rng.randrange(10**9, 10**12)
                i.out_octets = self.rng.randrange(10**9, 10**12)

    def _iface(self, dev: str, name: str, speed: int, desc: str, load: float) -> SimInterface:
        i = SimInterface(name, speed, desc, base_load=load)
        self.devices[dev].interfaces[name] = i
        return i

    def _log(self, dev: SimDevice, facility: str, severity: int, mnemonic: str, text: str) -> None:
        ts = time.strftime("%b %d %H:%M:%S", time.localtime(self.clock))
        if dev.vendor == "cisco_ios":
            line = f"{ts}: %{facility}-{severity}-{mnemonic}: {text}"
        else:
            line = f"{ts} {dev.name} %%01{facility}/{severity}/{mnemonic}(l):{text}"
        dev.syslog.append(line)

    def _set_link(self, dev: str, ifname: str, up: bool, reason: str = "") -> None:
        d = self.devices[dev]
        i = d.interfaces[ifname]
        was = i.oper_up
        i.link_up = up
        if was != i.oper_up and d.powered:
            state = "up" if i.oper_up else "down"
            if d.vendor == "cisco_ios":
                self._log(d, "LINK", 3, "UPDOWN", f"Interface {ifname}, changed state to {state}")
                self._log(d, "LINEPROTO", 5, "UPDOWN", f"Line protocol on Interface {ifname}, changed state to {state}")
            else:
                self._log(d, "IFNET", 2, "LINK_STATE", f"The physical state of interface {ifname} changed to {state.upper()}.{reason}")

    # -- simulation -----------------------------------------------------------

    def tick(self, seconds: float = 5.0) -> None:
        with self.lock:
            self.clock += seconds
            hour = time.localtime(self.clock).tm_hour
            # Business-hours curve so utilisation and its alerts have a shape.
            diurnal = 0.55 + 0.45 * max(0.0, min(1.0, (min(hour, 22) - 7) / 6)) if 7 <= hour <= 22 else 0.35
            for d in self.devices.values():
                if not d.powered:
                    continue
                d.uptime += seconds
                target = 85.0 + self.rng.uniform(0, 12) if d.cpu_fault else 6.0 + self.rng.uniform(0, 10)
                d.cpu = round(d.cpu + (target - d.cpu) * 0.5, 1)
                for i in d.interfaces.values():
                    if not i.oper_up:
                        continue
                    load = max(0.0, i.base_load * diurnal * self.rng.uniform(0.8, 1.2))
                    bps = i.speed_mbps * 1_000_000 * min(load, 0.98)
                    i.in_octets = (i.in_octets + int(bps / 8 * seconds)) % COUNTER_MAX
                    i.out_octets = (i.out_octets + int(bps / 8 * seconds * self.rng.uniform(0.6, 1.1))) % COUNTER_MAX
                    if i.error_rate:
                        n = int(i.error_rate * seconds * self.rng.uniform(0.7, 1.3))
                        i.in_errors += n
                        i.crc += n

    # -- fault injection --------------------------------------------------------

    def fail_link(self, dev: str, ifname: str) -> None:
        with self.lock:
            peer = self.devices[dev].interfaces[ifname].peer
            self._set_link(dev, ifname, False)
            if peer:
                self._set_link(*peer, False)

    def restore_link(self, dev: str, ifname: str) -> None:
        with self.lock:
            peer = self.devices[dev].interfaces[ifname].peer
            self._set_link(dev, ifname, True)
            if peer:
                self._set_link(*peer, True)

    def corrupt_port(self, dev: str, ifname: str, errors_per_sec: float = 40.0) -> None:
        with self.lock:
            self.devices[dev].interfaces[ifname].error_rate = errors_per_sec

    def cpu_spike(self, dev: str, on: bool = True) -> None:
        with self.lock:
            self.devices[dev].cpu_fault = on

    def power(self, dev: str, on: bool) -> None:
        with self.lock:
            d = self.devices[dev]
            if d.powered == on:
                return
            d.powered = on
            if not on:
                for i in d.interfaces.values():
                    if i.peer:
                        self._set_link(*i.peer, False, " (peer lost power)")
            else:
                d.uptime = 0
                for i in d.interfaces.values():
                    if i.peer and self.devices[i.peer[0]].powered:
                        self._set_link(*i.peer, True)
                self._log(d, "SYS", 5, "RESTART", "System restarted")

    def err_disable(self, dev: str, ifname: str) -> None:
        """What BPDU guard does when someone plugs a switch into a user port."""
        with self.lock:
            d = self.devices[dev]
            i = d.interfaces[ifname]
            i.err_disabled = True
            if d.vendor == "cisco_ios":
                self._log(d, "PM", 4, "ERR_DISABLE", f"bpduguard error detected on {ifname}, putting {ifname} in err-disable state")
            else:
                self._log(d, "MSTP", 4, "BPDU_PROTECTION", f"This edged-port {ifname} that enabled BPDU-Protection will be shutdown, because it received BPDU packet!")
            self._set_link(dev, ifname, i.link_up)

    def reachable(self, dev: str) -> bool:
        with self.lock:
            if not self.devices[dev].powered:
                return False
            seen = {a for a in MGMT_ANCHORS if a in self.devices and self.devices[a].powered}
            stack = list(seen)
            while stack:
                cur = stack.pop()
                if cur == dev:
                    return True
                for i in self.devices[cur].interfaces.values():
                    if i.peer and i.oper_up and i.peer[0] not in seen:
                        peer_dev = self.devices[i.peer[0]]
                        if peer_dev.powered and peer_dev.interfaces[i.peer[1]].oper_up:
                            seen.add(i.peer[0])
                            stack.append(i.peer[0])
            return dev in seen

    def heal(self) -> None:
        with self.lock:
            for d in self.devices.values():
                self.power(d.name, True)
                d.cpu_fault = False
                for name, i in d.interfaces.items():
                    i.error_rate = 0.0
                    i.err_disabled = False
                    if not i.link_up:
                        self._set_link(d.name, name, True)

    # -- device-facing protocol ---------------------------------------------------

    def drain_syslog(self, dev: str) -> list[str]:
        with self.lock:
            d = self.devices[dev]
            # Syslog travels the same network: an isolated device's messages
            # stay buffered on the box until the path comes back.
            if not self.reachable(dev):
                return []
            out, d.syslog = d.syslog, []
            return out

    def execute(self, dev: str, command: str) -> str:
        from fathom.devices import render  # local import: rendering is presentation, not simulation

        with self.lock:
            d = self.devices.get(dev)
            if d is None:
                raise KeyError(dev)
            if not self.reachable(dev):
                raise DeviceUnreachable(f"{dev} ({d.mgmt}) did not respond")
            return render.command(self, d, command.strip())

    def configure(self, dev: str, lines: list[str]) -> str:
        """Apply configuration lines with IOS/VRP-style context handling."""
        with self.lock:
            d = self.devices[dev]
            if not self.reachable(dev):
                raise DeviceUnreachable(f"{dev} ({d.mgmt}) did not respond")
            current: SimInterface | None = None
            applied = []
            for raw in lines:
                line = raw.strip()
                if not line or line in ("!", "#", "end", "return", "quit", "configure terminal", "system-view"):
                    continue
                low = line.lower()
                if low.startswith("interface "):
                    name = line.split(None, 1)[1]
                    current = d.interfaces.get(name)
                    if current is None:
                        raise ValueError(f"{dev}: no interface {name}")
                elif current and low in ("shutdown",):
                    current.admin_up = False
                    self._log(d, "LINK", 5, "CHANGED", f"Interface {current.name}, changed state to administratively down")
                elif current and low in ("no shutdown", "undo shutdown"):
                    current.admin_up = True
                    current.err_disabled = False
                    self._log(d, "LINK", 3, "UPDOWN", f"Interface {current.name}, changed state to up")
                elif current and (low.startswith("description ")):
                    current.description = line.split(None, 1)[1]
                elif current and low in ("no description", "undo description"):
                    current.description = ""
                elif low.startswith(("hostname ", "sysname ")):
                    d.hostname_cfg = line.split(None, 1)[1]
                else:
                    raise ValueError(f"{dev}: unsupported command in lab: {line!r}")
                applied.append(line)
            return "\n".join(applied)
