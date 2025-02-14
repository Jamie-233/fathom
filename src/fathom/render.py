"""Vendor CLI rendering for the lab.

Output mimics the real formats closely enough that the parsers are tested
against the same quirks they meet on hardware: IOS spreads one interface
over many lines with counters buried in prose, VRP's brief table abbreviates
state as ``*down`` for admin-down and ``^down`` for standby. A parser that
only ever saw tidy JSON would break on its first real device.
"""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .lab import Lab, SimDevice, SimInterface


def _ios_state(i: "SimInterface") -> tuple[str, str]:
    if not i.admin_up:
        return "administratively down", "down"
    if i.err_disabled:
        return "down", "down (err-disabled)"
    return ("up", "up") if i.link_up else ("down", "down")


def _uptime(seconds: float) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    return f"{days // 7} weeks, {days % 7} days, {hours} hours, {rem // 60} minutes"


def ios_show_interfaces(d: "SimDevice") -> str:
    blocks = []
    for i in d.interfaces.values():
        line, proto = _ios_state(i)
        blocks.append(
            f"{_ios_full(i.name)} is {line}, line protocol is {proto}\n"
            f"  Hardware is Gigabit Ethernet, address is 00a1.b2c3.{zlib.crc32(i.name.encode()) & 0xffff:04x}\n"
            f"  Description: {i.description}\n"
            f"  MTU 1500 bytes, BW {i.speed_mbps * 1000} Kbit/sec, DLY 10 usec,\n"
            f"     reliability 255/255, txload 1/255, rxload 1/255\n"
            f"     {i.in_octets // 900} packets input, {i.in_octets} bytes, 0 no buffer\n"
            f"     {i.in_errors} input errors, {i.crc} CRC, 0 frame, 0 overrun, 0 ignored\n"
            f"     {i.out_octets // 900} packets output, {i.out_octets} bytes, 0 underruns\n"
        )
    return "".join(blocks)


_IOS_LONG = {"Gi": "GigabitEthernet", "Te": "TenGigabitEthernet"}


def _ios_full(name: str) -> str:
    for short, long in _IOS_LONG.items():
        if name.startswith(short) and not name.startswith(long):
            return long + name[len(short):]
    return name


def vrp_interface_brief(d: "SimDevice") -> str:
    rows = [
        "PHY: Physical\n*down: administratively down\n^down: standby\n(e): ETHOAM down\n",
        "Interface                   PHY   Protocol  InUti OutUti   inErrors  outErrors",
    ]
    for i in d.interfaces.values():
        phy = "*down" if not i.admin_up else ("up" if i.oper_up else "down")
        proto = "up" if i.oper_up else "down"
        rows.append(f"{i.name:<27} {phy:<5} {proto:<9} {'0.01%':>5} {'0.01%':>6} {i.in_errors:>10} {0:>10}")
    return "\n".join(rows) + "\n"


def vrp_counters(d: "SimDevice") -> str:
    # VRP's brief table has no byte counters; real collectors pair it with a
    # per-interface counters view. One line per port keeps the parse cheap.
    return "\n".join(
        f"{i.name} input-bytes {i.in_octets} output-bytes {i.out_octets} crc {i.crc}"
        for i in d.interfaces.values()
    ) + "\n"


def running_config(d: "SimDevice") -> str:
    host = d.hostname_cfg or d.name
    if d.vendor == "cisco_ios":
        out = [f"hostname {host}", "!"]
        for i in d.interfaces.values():
            out.append(f"interface {i.name}")
            if i.description:
                out.append(f" description {i.description}")
            out.append(" shutdown" if not i.admin_up else " no shutdown")
            out.append("!")
        return "\n".join(out + ["end"]) + "\n"
    out = [f"sysname {host}", "#"]
    for i in d.interfaces.values():
        out.append(f"interface {i.name}")
        if i.description:
            out.append(f" description {i.description}")
        if not i.admin_up:
            out.append(" shutdown")
        out.append("#")
    return "\n".join(out + ["return"]) + "\n"


def version(d: "SimDevice") -> str:
    if d.vendor == "cisco_ios":
        return (f"Cisco IOS XE Software, Version 17.9.4\n{d.name} uptime is {_uptime(d.uptime)}\n"
                f"cisco {d.model} processor\n")
    return (f"Huawei Versatile Routing Platform Software\nVRP (R) software, Version 5.170 (V200R022C00)\n"
            f"HUAWEI {d.model} Routing Switch uptime is {_uptime(d.uptime)}\n")


def cpu(d: "SimDevice") -> str:
    if d.vendor == "cisco_ios":
        return f"CPU utilization for five seconds: {d.cpu:.0f}%/1%; one minute: {d.cpu:.0f}%; five minutes: {d.cpu:.0f}%\n"
    return f"CPU utilization for five seconds: {d.cpu:.0f}%: one minute: {d.cpu:.0f}%: five minutes: {d.cpu:.0f}%\n"


def lldp(lab: "Lab", d: "SimDevice") -> str:
    rows = []
    for i in d.interfaces.values():
        if i.peer and i.oper_up:
            rows.append(f"{i.name} {i.peer[0]} {i.peer[1]}")
    return "\n".join(rows) + "\n"


COMMANDS = {
    "cisco_ios": {
        "show interfaces": ios_show_interfaces,
        "show running-config": running_config,
        "show version": version,
        "show processes cpu": cpu,
    },
    "huawei_vrp": {
        "display interface brief": vrp_interface_brief,
        "display interface counters": vrp_counters,
        "display current-configuration": running_config,
        "display version": version,
        "display cpu-usage": cpu,
    },
}


def command(lab: "Lab", d: "SimDevice", cmd: str) -> str:
    if cmd in ("show lldp neighbors", "display lldp neighbor brief"):
        return lldp(lab, d)
    fn = COMMANDS[d.vendor].get(cmd)
    if fn is None:
        marker = "% Invalid input detected at '^' marker." if d.vendor == "cisco_ios" \
            else "Error: Unrecognized command found at '^' position."
        return f"{cmd}\n{'^':>1}\n{marker}\n"
    return fn(d)

