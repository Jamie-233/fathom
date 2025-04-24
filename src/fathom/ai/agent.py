"""The operations agent.

The agent sees the platform the way an on-call engineer does — incidents,
device and port state, recent logs, runbooks — and can run *read-only* show
commands. It cannot change anything. The one write it has is
``propose_port_change``, which queues a job that a human must approve; the
four-eyes rule in the remediator means the agent's proposal can never be
approved by the agent.

That boundary is the design. A model that can only read and propose gets
everything useful out of LLMs (correlation, explanation, drafting the fix)
while the failure mode of a confident wrong answer is a rejected job, not an
outage.

Without a model configured, :func:`explain_incident` produces the same kind
of answer deterministically from root cause analysis and the knowledge
base, so the feature degrades instead of disappearing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from fathom.observe.collector import COMMANDS
from fathom.devices.lab import DeviceUnreachable
from fathom.platform import Platform

SYSTEM = """You are Fathom's network operations assistant for a campus network.
Ground every statement in tool results; cite device and interface names.
Start from incidents and their root cause before looking at individual alerts.
Use runbooks for remediation advice. You may run read-only show commands.
You cannot change devices. If a change is needed, call propose_port_change;
a human will review it. Say plainly when evidence is insufficient.
Answer concisely in the user's language."""


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    fn: Callable[..., Any]

    def spec(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.params,
                           "required": [k for k, v in self.params.items() if not v.get("optional")],
                           "additionalProperties": False}}}


def build_tools(p: Platform) -> dict[str, Tool]:
    s = p.store

    def list_incidents(status: str = "open"):
        return s.rows("SELECT id, title, kind, confidence, status, impact FROM incidents WHERE status=? ORDER BY id DESC LIMIT 20",
                      (status,))

    def get_incident(incident_id: int):
        inc = s.row("SELECT * FROM incidents WHERE id=?", (incident_id,))
        if not inc:
            return {"error": f"no incident {incident_id}"}
        alerts = s.rows("SELECT rule, severity, device, target, summary, root, status FROM alerts WHERE incident_id=?",
                        (incident_id,))
        devices = {a["device"] for a in alerts} | {inc["root_device"]}
        marks = ",".join("?" * len(devices))
        events = s.rows(f"SELECT device, interface, state, message FROM events WHERE device IN ({marks}) "
                        "ORDER BY ts DESC LIMIT 10", tuple(devices))
        jobs = s.rows("SELECT id, kind, status FROM jobs WHERE alert_id IN (SELECT id FROM alerts WHERE incident_id=?)",
                      (incident_id,))
        return {"incident": inc, "alerts": alerts, "recent_logs": events, "jobs": jobs}

    def device_status(device: str):
        d = s.row("SELECT * FROM devices WHERE name=?", (device,))
        if not d:
            return {"error": f"unknown device {device}"}
        ports = s.rows("SELECT name, admin_up, oper_up, description, util, err_per_min FROM interfaces WHERE device=?",
                       (device,))
        notable = [x for x in ports if not x["oper_up"] or (x["err_per_min"] or 0) > 0 or (x["util"] or 0) > 60]
        return {"device": d, "ports_total": len(ports), "ports_down": sum(not x["oper_up"] for x in ports),
                "notable_ports": notable[:15]}

    def show_command(device: str, command: str):
        d = s.row("SELECT vendor FROM devices WHERE name=?", (device,))
        if not d:
            return {"error": f"unknown device {device}"}
        allowed = {c for v in COMMANDS[d["vendor"]].values() for c in (v if isinstance(v, tuple) else (v,))}
        if command not in allowed:
            return {"error": f"not an allowed read-only command; choose from {sorted(allowed)}"}
        try:
            out = p.transport.run(device, command)
        except DeviceUnreachable as exc:
            return {"error": str(exc)}
        return {"output": out[:2500] + ("\n…[truncated]" if len(out) > 2500 else "")}

    def search_runbooks(query: str):
        return [{"runbook": h.section.title, "section": h.section.heading, "text": h.section.text, "score": h.score}
                for h in p.kb.search(query, limit=3)]

    def propose_port_change(device: str, interface: str, action: str, reason: str):
        kind = {"enable": "port_enable", "disable": "port_disable"}.get(action)
        if not kind:
            return {"error": "action must be enable or disable"}
        try:
            jid = p.remediator.propose(kind, device, interface, reason, actor="agent")
        except (KeyError, ValueError) as exc:
            return {"error": str(exc)}
        return {"job_id": jid, "status": "pending_approval", "note": "a human must approve this job"}

    def string(desc: str, optional: bool = False, enum: list[str] | None = None):
        out = {"type": "string", "description": desc}
        if optional:
            out["optional"] = True
        if enum:
            out["enum"] = enum
        return out

    tools = [
        Tool("list_incidents", "List incidents with their root-cause title and impact. Start here.",
             {"status": string("open or closed", True, ["open", "closed"])}, list_incidents),
        Tool("get_incident", "Full detail of one incident: alerts (root vs symptom), recent logs, remediation jobs.",
             {"incident_id": {"type": "integer", "description": "Incident id"}}, get_incident),
        Tool("device_status", "Reachability, CPU, uptime and notable ports (down, errors, busy) of a device.",
             {"device": string("Device name, e.g. dist-01")}, device_status),
        Tool("show_command", "Run an allow-listed read-only command on a device and return raw output.",
             {"device": string("Device name"), "command": string("e.g. 'show interfaces' or 'display interface brief'")},
             show_command),
        Tool("search_runbooks", "Search the team's runbooks for diagnosis and resolution steps.",
             {"query": string("Keywords, e.g. 'CRC errors cable'")}, search_runbooks),
        Tool("propose_port_change", "Queue enabling/disabling a user port for human approval. Never executes directly.",
             {"device": string("Device"), "interface": string("Interface, e.g. Gi1/0/3"),
              "action": string("enable or disable", enum=["enable", "disable"]),
              "reason": string("Why, citing evidence")}, propose_port_change),
    ]
    return {t.name: t for t in tools}


def _schema(tools: dict[str, Tool]) -> list[dict]:
    specs = []
    for t in tools.values():
        spec = t.spec()
        # "optional" is our marker for building `required`; it is not JSON Schema.
        for v in spec["function"]["parameters"]["properties"].values():
            v.pop("optional", None)
        specs.append(spec)
    return specs


@dataclass
class Step:
    tool: str
    args: dict
    result: Any


@dataclass
class Answer:
    text: str
    steps: list[Step] = field(default_factory=list)
    mode: str = "llm"


class Agent:
    def __init__(self, platform: Platform, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None, max_turns: int = 8):
        self.p = platform
        self.tools = build_tools(platform)
        self.base_url = (base_url or os.environ.get("FATHOM_LLM_BASE_URL") or "").rstrip("/")
        self.model = model or os.environ.get("FATHOM_LLM_MODEL", "gpt-4o-mini")
        self.api_key = api_key or os.environ.get("FATHOM_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self.max_turns = max_turns

    @property
    def online(self) -> bool:
        return bool(self.base_url)

    def _complete(self, messages: list[dict], tools: list[dict]) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "messages": messages, "temperature": 0.1}
        if tools:
            body["tools"] = tools
        r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=120)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]

    def call(self, name: str, args: dict) -> Any:
        tool = self.tools.get(name)
        if not tool:
            return {"error": f"no tool {name}"}
        try:
            return tool.fn(**args)
        except TypeError as exc:
            return {"error": f"bad arguments: {exc}"}

    def ask(self, question: str) -> Answer:
        if not self.online:
            return self.offline(question)
        messages: list[dict] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
        steps: list[Step] = []
        schema = _schema(self.tools)
        for turn in range(self.max_turns):
            last = turn == self.max_turns - 1
            msg = self._complete(messages, [] if last else schema)
            calls = msg.get("tool_calls") or []
            if not calls:
                return Answer((msg.get("content") or "").strip(), steps)
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args, result = {}, {"error": "arguments were not valid JSON"}
                else:
                    result = self.call(c["function"]["name"], args)
                steps.append(Step(c["function"]["name"], args, result))
                self.p.store.audit("agent", "tool." + c["function"]["name"], args.get("device", ""), args)
                payload = json.dumps(result, ensure_ascii=False, default=str)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": payload[:4000]})
        return Answer("I could not reach a conclusion within the step limit.", steps)

    def offline(self, question: str) -> Answer:
        incidents = self.call("list_incidents", {})
        if not incidents:
            return Answer("No open incidents. The network looks healthy from the poller's point of view.",
                          [Step("list_incidents", {}, incidents)], mode="offline")
        return explain_incident(self, incidents[0]["id"])


def explain_incident(agent: Agent, incident_id: int) -> Answer:
    detail = agent.call("get_incident", {"incident_id": incident_id})
    inc = detail["incident"]
    roots = [a for a in detail["alerts"] if a["root"]]
    symptoms = [a for a in detail["alerts"] if not a["root"]]
    query = " ".join([inc["title"], *(a["rule"].replace("_", " ") for a in roots)])
    hits = agent.call("search_runbooks", {"query": query})
    lines = [f"**Incident #{inc['id']}: {inc['title']}** (confidence: {inc['confidence']})", ""]
    if roots:
        lines.append("Root evidence: " + "; ".join(a["summary"] for a in roots) + ".")
    if symptoms:
        lines.append(f"{len(symptoms)} related alert(s) are symptoms of this root and were grouped into it.")
    if inc["impact"]:
        lines.append(f"Impact: {inc['impact'].replace(',', ', ')} cannot be reached.")
    if detail["jobs"]:
        lines.append("Remediation: " + ", ".join(f"job #{j['id']} {j['kind']} ({j['status']})" for j in detail["jobs"]) + ".")
    if hits:
        best = next((h for h in hits if h["section"] in ("Resolution", "Diagnosis")), hits[0])
        lines += ["", f"From runbook *{best['runbook']}* — {best['section']}:", best["text"]]
    return Answer("\n".join(lines), [Step("get_incident", {"incident_id": incident_id}, detail),
                                     Step("search_runbooks", {"query": query}, hits)], mode="offline")

