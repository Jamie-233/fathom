"""Transports: the only way the platform reaches devices.

Every consumer — collector, remediation, IaC, the agent — depends on three
methods and nothing else. That narrow surface is what lets the same code run
against the simulated lab in tests and demos, and against real switches over
SSH in production.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Protocol

from fathom.devices.lab import DeviceUnreachable, Lab


class Transport(Protocol):
    def run(self, device: str, command: str) -> str: ...
    def configure(self, device: str, lines: list[str]) -> str: ...
    def syslog(self, device: str) -> list[str]: ...


class LabTransport:
    def __init__(self, lab: Lab):
        self.lab = lab

    def run(self, device: str, command: str) -> str:
        return self.lab.execute(device, command)

    def configure(self, device: str, lines: list[str]) -> str:
        return self.lab.configure(device, lines)

    def syslog(self, device: str) -> list[str]:
        return self.lab.drain_syslog(device)


class SSHTransport:
    """Netmiko-backed transport for real devices.

    Sessions are cached per device because logging in is the slowest part of
    a poll, often seconds through AAA. Syslog isn't pulled over SSH in
    production; it arrives at a collector, so this returns what an external
    receiver has queued via :meth:`push_syslog`.
    """

    _NETMIKO = {"cisco_ios": "cisco_ios", "huawei_vrp": "huawei"}

    def __init__(self, inventory: dict[str, dict]):
        self.inventory = inventory
        self._sessions: dict[str, object] = {}
        self._logs: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def _session(self, device: str):
        if device in self._sessions:
            return self._sessions[device]
        try:
            from netmiko import ConnectHandler
            from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("SSH transport needs netmiko: pip install netmiko") from exc
        d = self.inventory[device]
        user = os.environ.get("FATHOM_SSH_USER")
        password = os.environ.get("FATHOM_SSH_PASSWORD")
        if not user or password is None:
            raise RuntimeError("SSH transport needs FATHOM_SSH_USER and FATHOM_SSH_PASSWORD")
        try:
            conn = ConnectHandler(device_type=self._NETMIKO[d["vendor"]], host=d["mgmt"],
                                  username=user, password=password,
                                  read_timeout_override=60)
        except (NetmikoTimeoutException, NetmikoAuthenticationException, OSError) as exc:
            raise DeviceUnreachable(f"{device}: {exc}") from exc
        self._sessions[device] = conn
        return conn

    def run(self, device: str, command: str) -> str:
        try:
            return self._session(device).send_command(command)
        except OSError as exc:
            self._sessions.pop(device, None)  # stale session: reconnect next time
            raise DeviceUnreachable(f"{device}: {exc}") from exc

    def configure(self, device: str, lines: list[str]) -> str:
        conn = self._session(device)
        out = conn.send_config_set(lines)
        return out + "\n" + conn.save_config()

    def push_syslog(self, device: str, line: str) -> None:
        with self._lock:
            self._logs.setdefault(device, []).append(line)

    def syslog(self, device: str) -> list[str]:
        with self._lock:
            return self._logs.pop(device, [])


def load_inventory(path: str | Path) -> dict[str, dict]:
    """Read the device list from the same topology file the lab uses.

    The topology already carries name, vendor and management address, so a
    real deployment only has to point at its own copy — there is no second
    inventory to keep in sync with the one the lab reads.
    """
    import yaml

    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return {d["name"]: {"vendor": d["vendor"], "mgmt": d["mgmt"], "model": d.get("model", "")}
            for d in data["devices"]}


def build_transport(kind: str, lab: Lab, topology: str | Path) -> Transport:
    """Pick the transport by name. ``lab`` keeps demos and tests hermetic."""
    if kind == "lab":
        return LabTransport(lab)
    if kind == "ssh":
        return SSHTransport(load_inventory(topology))
    raise ValueError(f"unknown transport {kind!r}; use 'lab' or 'ssh'")
