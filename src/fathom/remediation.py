"""Event-driven remediation.

Runbooks turn an alert into a concrete job: the exact commands, how success
will be verified, and how risky it is. Risk decides who presses the button:

* **low** — reversible, local and well understood (re-enabling a port that
  BPDU guard shut). Runs automatically.
* **medium** — changes what users experience (shutting a port that is
  spewing errors). Waits for a human to approve.
* **read-only** — collects evidence; always automatic.

Automation that can't stop itself is how small outages become large ones.
If a port keeps getting err-disabled, something is plugged in that keeps
sending BPDUs, and bouncing it forever just causes a loop every few minutes.
So each runbook has an attempt budget per target; once spent, the next job
is created as "needs approval" even if the runbook is normally automatic.

Every job is verified by reading the device back. A command that was
accepted is not the same as a change that took effect.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from .collector import COMMANDS
from .lab import DeviceUnreachable
from .parsers import parse_ios_interfaces, parse_vrp_brief
from .store import Store


@dataclass
class Runbook:
    name: str
    rule: str
    risk: str                                   # low | medium | read-only
    title: Callable[[dict], str]
    commands: Callable[[str, dict], list[str]]  # (vendor, alert) -> commands
    verify: Callable[[dict], bool] | None       # interface state after -> ok
    applies: Callable[[Store, dict], bool] = lambda s, a: True
    max_auto_per_hour: int = 2


def _no_shut(vendor: str) -> str:
    return "no shutdown" if vendor == "cisco_ios" else "undo shutdown"


def _is_user_port(store: Store, alert: dict) -> bool:
    return store.row("SELECT 1 FROM links WHERE a=? AND a_if=?", (alert["device"], alert["target"])) is None


RUNBOOKS = [
    Runbook(
        "recover_err_disabled", "port_err_disabled", "low",
        title=lambda a: f"Re-enable {a['device']} {a['target']} after BPDU guard",
        commands=lambda v, a: [f"interface {a['target']}", "shutdown", _no_shut(v)],
        verify=lambda st: st["oper_up"],
    ),
    Runbook(
        "isolate_noisy_port", "interface_errors", "medium",
        title=lambda a: f"Shut {a['device']} {a['target']} (sustained CRC errors) and label it",
        commands=lambda v, a: [f"interface {a['target']}", "description fathom-isolated-crc-errors", "shutdown"],
        verify=lambda st: not st["admin_up"],
        # Shutting an uplink to stop errors would trade a degraded link for an
        # outage; infrastructure links get an incident and a human instead.
        applies=_is_user_port,
    ),
    # Manual actions: never triggered by an alert, only proposed by a person
    # or the agent, and always behind approval.
    Runbook(
        "port_disable", "", "medium",
        title=lambda a: f"Disable {a['device']} {a['target']}",
        commands=lambda v, a: [f"interface {a['target']}", "shutdown"],
        verify=lambda st: not st["admin_up"],
    ),
    Runbook(
        "port_enable", "", "medium",
        title=lambda a: f"Enable {a['device']} {a['target']}",
        commands=lambda v, a: [f"interface {a['target']}", _no_shut(v)],
        verify=lambda st: st["admin_up"],
    ),
    Runbook(
        "collect_cpu_diagnostics", "high_cpu", "read-only",
        title=lambda a: f"Collect CPU diagnostics from {a['device']}",
        commands=lambda v, a: [COMMANDS[v]["cpu"], COMMANDS[v]["version"]],
        verify=None,
    ),
]


class Remediator:
    def __init__(self, store: Store, transport, clock=time.time, runbooks: list[Runbook] | None = None):
        self.store = store
        self.transport = transport
        self.clock = clock
        books = runbooks or RUNBOOKS
        self.runbooks = {r.rule: r for r in books if r.rule}
        self.by_name = {r.name: r for r in books}

    def on_alerts(self, alert_ids: list[int]) -> list[int]:
        created = []
        for aid in alert_ids:
            a = self.store.row("SELECT * FROM alerts WHERE id=?", (aid,))
            rb = self.runbooks.get(a["rule"]) if a else None
            if not rb or not rb.applies(self.store, a):
                continue
            dev = self.store.row("SELECT vendor FROM devices WHERE name=?", (a["device"],))
            recent = self.store.row(
                "SELECT COUNT(*) n FROM jobs WHERE kind=? AND device=? AND target=? AND created > ? AND requested_by='fathom'",
                (rb.name, a["device"], a["target"], self.clock() - 3600))["n"]
            auto = rb.risk in ("low", "read-only") and recent < rb.max_auto_per_hour
            status = "approved" if auto else "pending_approval"
            plan = {"commands": rb.commands(dev["vendor"], a), "risk": rb.risk,
                    "reason": a["summary"], "escalated": rb.risk == "low" and not auto}
            cur = self.store.execute(
                "INSERT INTO jobs (kind, title, device, target, status, created, requested_by, approved_by, plan, alert_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rb.name, rb.title(a), a["device"], a["target"], status, self.clock(), "fathom",
                 "policy" if auto else None, json.dumps(plan), aid))
            created.append(cur.lastrowid)
            self.store.audit("fathom", "job.created", a["device"], {"job": cur.lastrowid, "status": status})
        return created

    def propose(self, kind: str, device: str, target: str, reason: str, actor: str) -> int:
        """Queue a manual action. It always waits for approval, whoever asks."""
        rb = self.by_name[kind]
        dev = self.store.row("SELECT vendor FROM devices WHERE name=?", (device,))
        if not dev:
            raise KeyError(f"unknown device {device}")
        if not self.store.row("SELECT 1 FROM interfaces WHERE device=? AND name=?", (device, target)):
            raise KeyError(f"unknown interface {device} {target}")
        if self.store.row("SELECT 1 FROM links WHERE a=? AND a_if=?", (device, target)) and kind == "port_disable":
            raise ValueError(f"{device} {target} is an infrastructure link; refusing to queue a shutdown")
        ctx = {"device": device, "target": target, "summary": reason}
        plan = {"commands": rb.commands(dev["vendor"], ctx), "risk": rb.risk, "reason": reason}
        cur = self.store.execute(
            "INSERT INTO jobs (kind, title, device, target, status, created, requested_by, plan) "
            "VALUES (?,?,?,?, 'pending_approval', ?,?,?)",
            (kind, rb.title(ctx), device, target, self.clock(), actor, json.dumps(plan)))
        self.store.audit(actor, "job.proposed", device, {"job": cur.lastrowid, "reason": reason})
        return cur.lastrowid

    def approve(self, job_id: int, actor: str) -> dict:
        return self._decide(job_id, actor, "approved")

    def reject(self, job_id: int, actor: str) -> dict:
        return self._decide(job_id, actor, "rejected")

    def _decide(self, job_id: int, actor: str, status: str) -> dict:
        job = self.store.row("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job:
            raise KeyError(job_id)
        if job["status"] != "pending_approval":
            raise ValueError(f"job {job_id} is {job['status']}, not awaiting approval")
        if status == "approved" and actor == job["requested_by"]:
            # Four-eyes: whoever asked for a change cannot also approve it.
            raise PermissionError(f"{actor} requested job {job_id} and cannot approve it")
        self.store.execute("UPDATE jobs SET status=?, approved_by=? WHERE id=?", (status, actor, job_id))
        self.store.audit(actor, f"job.{status}", job["device"], {"job": job_id})
        return self.store.row("SELECT * FROM jobs WHERE id=?", (job_id,))

    def run_approved(self) -> list[int]:
        done = []
        for job in self.store.rows("SELECT * FROM jobs WHERE status='approved' ORDER BY id"):
            self._run(job)
            done.append(job["id"])
        return done

    def _interface_state(self, device: str, vendor: str, ifname: str) -> dict | None:
        outs = [self.transport.run(device, c) for c in COMMANDS[vendor]["interfaces"]]
        stats = parse_ios_interfaces(outs[0]) if vendor == "cisco_ios" else parse_vrp_brief(*outs)
        st = next((s for s in stats if s.name == ifname), None)
        return None if st is None else {"oper_up": st.oper_up, "admin_up": st.admin_up}

    def _run(self, job: dict) -> None:
        rb = self.by_name[job["kind"]]
        plan = json.loads(job["plan"])
        vendor = self.store.row("SELECT vendor FROM devices WHERE name=?", (job["device"],))["vendor"]
        self.store.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
        actor = job["approved_by"] if job["approved_by"] not in (None, "policy") else "fathom-auto"
        try:
            if rb.risk == "read-only":
                output = "\n".join(f"$ {c}\n{self.transport.run(job['device'], c)}" for c in plan["commands"])
                ok = True
            else:
                output = self.transport.configure(job["device"], plan["commands"])
                state = self._interface_state(job["device"], vendor, job["target"])
                ok = bool(state) and rb.verify(state)
                output += f"\n-- verification: {state} -> {'ok' if ok else 'FAILED'}"
        except (DeviceUnreachable, ValueError) as exc:
            ok, output = False, f"{type(exc).__name__}: {exc}"
        status = "succeeded" if ok else "failed"
        self.store.execute("UPDATE jobs SET status=?, finished=?, output=? WHERE id=?",
                           (status, self.clock(), output, job["id"]))
        self.store.audit(actor, f"job.{status}", job["device"], {"job": job["id"], "commands": plan["commands"]})
