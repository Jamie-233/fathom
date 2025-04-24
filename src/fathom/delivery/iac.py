"""Declarative configuration: desired state → plan → apply.

The model is Terraform's, applied to switches. The desired state lives in a
YAML file under version control; ``plan`` reads what the devices are running
*now* and computes the minimal set of changes; ``apply`` pushes exactly that
plan and then reads the devices back to prove it took effect.

Three choices matter:

* **Diff against the device, not against the last apply.** A previous run's
  record says nothing about the engineer who changed a port by hand last
  night. Reading live state is what makes drift visible.
* **Only declared fields are managed.** An interface not in the file, or a
  field not set for it, is left alone. Otherwise adopting the tool would
  mean declaring the entire network on day one.
* **Apply refuses a stale plan.** If the device changed between plan and
  apply, the reviewed plan no longer describes what would happen, so the
  apply is rejected and has to be re-planned and re-reviewed.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from fathom.observe.collector import COMMANDS
from fathom.devices.lab import DeviceUnreachable
from fathom.data.parsers import parse_ios_interfaces, parse_vrp_brief
from fathom.data.store import Store

TEMPLATES = Path(__file__).parent.parent / "config" / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)


class PlanError(ValueError):
    pass


@dataclass
class Change:
    interface: str
    field: str
    before: object
    after: object


@dataclass
class DevicePlan:
    device: str
    vendor: str
    changes: list[Change] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    fingerprint: str = ""
    error: str = ""


@dataclass
class Plan:
    devices: list[DevicePlan]

    @property
    def change_count(self) -> int:
        return sum(len(d.changes) for d in self.devices)

    def to_dict(self) -> dict:
        return {"change_count": self.change_count, "devices": [asdict(d) for d in self.devices]}


def load_desired(path: str | Path) -> dict:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return parse_desired(doc)


def parse_desired(doc: dict) -> dict:
    devices = doc.get("devices")
    if not isinstance(devices, dict):
        raise PlanError("desired state needs a 'devices' mapping")
    for dev, spec in devices.items():
        for ifname, attrs in (spec or {}).get("interfaces", {}).items():
            unknown = set(attrs or {}) - {"description", "enabled"}
            if unknown:
                raise PlanError(f"{dev} {ifname}: unsupported field(s) {sorted(unknown)}")
    return devices


def _live_state(transport, device: str, vendor: str) -> dict[str, dict]:
    outs = [transport.run(device, c) for c in COMMANDS[vendor]["interfaces"]]
    stats = parse_ios_interfaces(outs[0]) if vendor == "cisco_ios" else parse_vrp_brief(*outs)
    if vendor == "huawei_vrp":
        # VRP's brief table truncates descriptions, so read them from config.
        cfg = transport.run(device, "display current-configuration")
        descs = _vrp_descriptions(cfg)
        for s in stats:
            s.description = descs.get(s.name, "")
    return {s.name: {"description": s.description, "enabled": s.admin_up} for s in stats}


def _vrp_descriptions(cfg: str) -> dict[str, str]:
    out, current = {}, None
    for line in cfg.splitlines():
        if line.startswith("interface "):
            current = line.split(None, 1)[1]
        elif current and line.startswith(" description "):
            out[current] = line[len(" description "):]
        elif line.startswith("#"):
            current = None
    return out


def _fingerprint(state: dict) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]


def plan(store: Store, transport, desired: dict) -> Plan:
    inventory = {d["name"]: d for d in store.rows("SELECT name, vendor FROM devices")}
    result = []
    for dev, spec in sorted(desired.items()):
        if dev not in inventory:
            raise PlanError(f"{dev} is not in the inventory")
        vendor = inventory[dev]["vendor"]
        dp = DevicePlan(dev, vendor)
        try:
            live = _live_state(transport, dev, vendor)
        except DeviceUnreachable as exc:
            dp.error = str(exc)
            result.append(dp)
            continue
        dp.fingerprint = _fingerprint(live)
        for ifname, want in sorted((spec or {}).get("interfaces", {}).items()):
            if ifname not in live:
                raise PlanError(f"{dev}: interface {ifname} does not exist on the device")
            for key in ("description", "enabled"):
                if key in (want or {}) and want[key] != live[ifname][key]:
                    dp.changes.append(Change(ifname, key, live[ifname][key], want[key]))
        if dp.changes:
            text = _env.get_template(f"{vendor}.j2").render(changes=dp.changes)
            dp.commands = [line for line in text.splitlines() if line.strip()]
        result.append(dp)
    return Plan(result)


def apply(store: Store, transport, reviewed: Plan, actor: str = "iac") -> list[dict]:
    """Apply a plan; returns one result per device with changes."""
    def run(dp: DevicePlan) -> dict:
        if dp.error:
            return {"device": dp.device, "ok": False, "detail": f"skipped: {dp.error}"}
        live = _live_state(transport, dp.device, dp.vendor)
        if _fingerprint(live) != dp.fingerprint:
            return {"device": dp.device, "ok": False, "detail": "device changed since plan; re-run plan"}
        transport.configure(dp.device, dp.commands)
        after = _live_state(transport, dp.device, dp.vendor)
        missing = [c for c in dp.changes if after[c.interface][c.field] != c.after]
        store.audit(actor, "config.apply", dp.device, {"commands": dp.commands, "verified": not missing})
        if missing:
            return {"device": dp.device, "ok": False,
                    "detail": f"{len(missing)} change(s) not reflected after apply"}
        return {"device": dp.device, "ok": True, "detail": f"{len(dp.changes)} change(s) applied and verified"}

    todo = [d for d in reviewed.devices if d.changes or d.error]
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(run, todo))
