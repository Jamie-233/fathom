"""Configuration backup, change detection and restore.

A backup is only stored when the configuration actually changed, keyed by a
hash of its normalised text. Storing every nightly copy of an unchanged
config buries the one version that matters under hundreds of identical ones,
and makes "what changed and when" a search instead of a list.

Restore is not "paste the old config back". Replaying a full config onto a
running device merges rather than replaces — lines removed since the backup
stay — and re-sends thousands of lines that didn't change. Instead the
backup is turned into a desired state and goes through the same plan/apply
path as any other change: minimal diff, stale-plan protection, verification,
audit.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import time

from fathom.data.parsers import canonical_ifname
from fathom.data.store import Store
from fathom.devices.lab import DeviceUnreachable
from fathom.delivery import iac

SHOW_CONFIG = {"cisco_ios": "show running-config", "huawei_vrp": "display current-configuration"}

# Lines that change on every read without any configuration change.
_VOLATILE = re.compile(r"^(! Last configuration change|! NVRAM config last updated|Building configuration|"
                       r"Current configuration :|ntp clock-period)")


def normalise(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines() if not _VOLATILE.match(line)).strip() + "\n"


def backup_all(store: Store, transport, clock=time.time) -> dict[str, str]:
    """Back up every device; returns device -> 'changed' | 'unchanged' | 'unreachable'."""
    result = {}
    for d in store.rows("SELECT name, vendor FROM devices ORDER BY name"):
        try:
            text = normalise(transport.run(d["name"], SHOW_CONFIG[d["vendor"]]))
        except DeviceUnreachable:
            result[d["name"]] = "unreachable"
            continue
        sha = hashlib.sha256(text.encode()).hexdigest()[:12]
        last = store.row("SELECT sha FROM config_backups WHERE device=? ORDER BY id DESC LIMIT 1", (d["name"],))
        if last and last["sha"] == sha:
            result[d["name"]] = "unchanged"
            continue
        store.execute("INSERT INTO config_backups (device, ts, sha, config) VALUES (?,?,?,?)",
                      (d["name"], clock(), sha, text))
        result[d["name"]] = "changed" if last else "initial"
    return result


def history(store: Store, device: str) -> list[dict]:
    return store.rows("SELECT id, ts, sha FROM config_backups WHERE device=? ORDER BY id DESC", (device,))


def diff(store: Store, old_id: int, new_id: int) -> str:
    a = store.row("SELECT device, sha, config FROM config_backups WHERE id=?", (old_id,))
    b = store.row("SELECT device, sha, config FROM config_backups WHERE id=?", (new_id,))
    if not a or not b:
        raise KeyError("backup not found")
    return "".join(difflib.unified_diff(a["config"].splitlines(True), b["config"].splitlines(True),
                                        fromfile=f"{a['device']}@{a['sha']}", tofile=f"{b['device']}@{b['sha']}"))


def desired_from_backup(config: str) -> dict:
    """Recover managed interface state (description, enabled) from a saved config."""
    interfaces: dict[str, dict] = {}
    current = None
    for line in config.splitlines():
        if line.startswith("interface "):
            current = canonical_ifname(line.split(None, 1)[1])
            interfaces[current] = {"description": "", "enabled": True}
        elif current and line.startswith(" description "):
            interfaces[current]["description"] = line[len(" description "):]
        elif current and line.strip() == "shutdown":
            interfaces[current]["enabled"] = False
        elif not line.startswith(" "):
            current = None
    return {"interfaces": interfaces}


def restore_plan(store: Store, transport, backup_id: int) -> iac.Plan:
    b = store.row("SELECT device, config FROM config_backups WHERE id=?", (backup_id,))
    if not b:
        raise KeyError("backup not found")
    return iac.plan(store, transport, {b["device"]: desired_from_backup(b["config"])})
