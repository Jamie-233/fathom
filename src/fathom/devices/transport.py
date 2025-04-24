"""Transports: the only way the platform reaches devices.

Every consumer — collector, remediation, IaC, the agent — depends on three
methods and nothing else. That narrow surface is what lets the same code run
against the simulated lab in tests and demos, and against real switches over
SSH in production.
"""

from __future__ import annotations

import os
import threading
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
        try:
            conn = ConnectHandler(device_type=self._NETMIKO[d["vendor"]], host=d["mgmt"],
                                  username=os.environ["FATHOM_SSH_USER"], password=os.environ["FATHOM_SSH_PASSWORD"],
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
