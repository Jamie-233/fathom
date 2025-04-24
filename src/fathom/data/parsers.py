"""Turn vendor CLI text into structured records.

Everything downstream — metrics, alerts, root cause, the agent — works on
these records, never on raw text, so vendor differences stop here. Two rules
make that hold:

* **Canonical interface names.** IOS prints ``GigabitEthernet1/0/48`` in
  ``show interfaces`` but ``Gi1/0/48`` in syslog and LLDP. If both forms
  reach the database, one physical port becomes two series and an alert can
  never be correlated with its own log message. Every name passes through
  :func:`canonical_ifname`.

* **Fail loudly on shape, quietly on content.** A block that doesn't match
  the expected structure raises — an image upgrade that changes the format
  must be noticed, not silently produce zero rows. A missing optional field
  (no description) is just ``""``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_LONG_TO_SHORT = [
    ("TenGigabitEthernet", "Te"),
    ("GigabitEthernet", "Gi"),
    ("FastEthernet", "Fa"),
    ("XGigabitEthernet", "XGE"),
    ("Ethernet", "Eth"),
]


def canonical_ifname(name: str) -> str:
    name = name.strip()
    for long, short in _LONG_TO_SHORT:
        if name.startswith(long):
            return short + name[len(long):]
    return name


class ParseError(ValueError):
    pass


@dataclass
class InterfaceStat:
    name: str
    admin_up: bool
    oper_up: bool
    in_bytes: int | None
    out_bytes: int | None
    in_errors: int
    crc: int
    description: str = ""
    speed_mbps: int | None = None
    err_disabled: bool = False


_IOS_HEAD = re.compile(r"^(\S+) is (administratively down|up|down), line protocol is (up|down)( \(err-disabled\))?", re.M)


def parse_ios_interfaces(text: str) -> list[InterfaceStat]:
    heads = list(_IOS_HEAD.finditer(text))
    if not heads and text.strip():
        raise ParseError("no interface blocks found in 'show interfaces' output")
    out = []
    for n, m in enumerate(heads):
        block = text[m.end(): heads[n + 1].start() if n + 1 < len(heads) else len(text)]

        def num(pattern: str) -> int:
            hit = re.search(pattern, block)
            if not hit:
                raise ParseError(f"{m.group(1)}: expected /{pattern}/ in interface block")
            return int(hit.group(1))

        bw = re.search(r"BW (\d+) Kbit", block)
        desc = re.search(r"^\s+Description: ?(.*)$", block, re.M)
        out.append(InterfaceStat(
            name=canonical_ifname(m.group(1)),
            admin_up=m.group(2) != "administratively down",
            oper_up=m.group(2) == "up" and m.group(3) == "up",
            in_bytes=num(r"packets input, (\d+) bytes"),
            out_bytes=num(r"packets output, (\d+) bytes"),
            in_errors=num(r"(\d+) input errors"),
            crc=num(r"(\d+) CRC"),
            description=desc.group(1).strip() if desc else "",
            speed_mbps=int(bw.group(1)) // 1000 if bw else None,
            err_disabled=bool(m.group(4)),
        ))
    return out


_VRP_ROW = re.compile(r"^(\S+)\s+(\*?\^?\w+(?:\(\w+\))?)\s+(\*?\w+(?:\(\w+\))?)\s+\S+\s+\S+\s+(\d+)\s+(\d+)\s*$")


def parse_vrp_brief(brief: str, counters: str = "") -> list[InterfaceStat]:
    """``display interface brief`` gives state and errors; byte counters come
    from a second command and are merged by name. Without them the bytes stay
    ``None`` — "unknown" — rather than 0, which a rate calculation would read
    as a counter reset."""
    header_seen = False
    rows: dict[str, InterfaceStat] = {}
    for line in brief.splitlines():
        if line.startswith("Interface") and "PHY" in line:
            header_seen = True
            continue
        if not header_seen or not line.strip():
            continue
        m = _VRP_ROW.match(line)
        if not m:
            raise ParseError(f"unexpected row in 'display interface brief': {line!r}")
        name, phy, proto, in_err, _ = m.groups()
        rows[name] = InterfaceStat(
            name=canonical_ifname(name), admin_up=not phy.startswith("*"),
            oper_up=phy == "up" and proto.startswith("up"),
            in_bytes=None, out_bytes=None, in_errors=int(in_err), crc=0,
        )
    if not header_seen and brief.strip():
        raise ParseError("no header in 'display interface brief' output")
    for line in counters.splitlines():
        m = re.match(r"^(\S+) input-bytes (\d+) output-bytes (\d+) crc (\d+)", line)
        if m and m.group(1) in rows:
            r = rows[m.group(1)]
            r.in_bytes, r.out_bytes, r.crc = int(m.group(2)), int(m.group(3)), int(m.group(4))
    return list(rows.values())


def parse_cpu(text: str) -> float:
    m = re.search(r"five seconds: (\d+)%", text)
    if not m:
        raise ParseError("no CPU figure found")
    return float(m.group(1))


def parse_uptime_seconds(text: str) -> int | None:
    m = re.search(r"uptime is (.+)", text)
    if not m:
        return None
    units = {"year": 31536000, "week": 604800, "day": 86400, "hour": 3600, "minute": 60}
    total = 0
    for value, unit in re.findall(r"(\d+) (year|week|day|hour|minute)s?", m.group(1)):
        total += int(value) * units[unit]
    return total


def parse_lldp(text: str) -> list[tuple[str, str, str]]:
    """Local port, neighbour device, neighbour port."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3:
            out.append((canonical_ifname(parts[0]), parts[1], canonical_ifname(parts[2])))
    return out


@dataclass
class LogEvent:
    device: str
    severity: int
    facility: str
    mnemonic: str
    message: str
    interface: str | None = None
    state: str | None = None

    @property
    def kind(self) -> str:
        if self.state is not None and self.interface:
            return "link_state"
        if self.mnemonic == "RESTART":
            return "restart"
        return "other"


_IOS_LOG = re.compile(r"%(?P<fac>[A-Z0-9_]+)-(?P<sev>\d)-(?P<mn>[A-Z0-9_]+): (?P<msg>.*)$")
_VRP_LOG = re.compile(r"%%\d+(?P<fac>[A-Z0-9_]+)/(?P<sev>\d)/(?P<mn>[A-Z0-9_]+)\(\w\):(?P<msg>.*)$")
_IFACE_STATE = [
    re.compile(r"Interface (?P<if>\S+), changed state to (?P<st>administratively down|up|down)"),
    re.compile(r"interface (?P<if>\S+) changed to (?P<st>UP|DOWN)", re.I),
]


def parse_syslog(device: str, line: str) -> LogEvent | None:
    m = _IOS_LOG.search(line) or _VRP_LOG.search(line)
    if not m:
        return None
    msg = m.group("msg").strip()
    ev = LogEvent(device, int(m.group("sev")), m.group("fac"), m.group("mn"), msg)
    # LINEPROTO duplicates LINK for the same transition; keeping only the
    # physical event stops every flap from being counted twice.
    if ev.facility == "LINEPROTO":
        return ev
    for pat in _IFACE_STATE:
        s = pat.search(msg)
        if s:
            ev.interface = canonical_ifname(s.group("if").rstrip(",."))
            ev.state = "down" if "down" in s.group("st").lower() else "up"
            break
    return ev
