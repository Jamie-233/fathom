"""REST API and portal hosting.

Reads are open; every write requires a bearer token when ``FATHOM_TOKEN`` is
set, and records the acting user from ``X-Fathom-User`` in the audit trail.
That user is also what the four-eyes rule compares, so approving your own
proposal is refused whether it comes from the portal or a script.

Plan/apply is two calls on purpose. ``plan`` returns an id; ``apply`` takes
that id, never a fresh desired state. What gets applied is exactly what was
reviewed — and if the devices changed in between, the stored plan's
fingerprints no longer match and apply refuses.
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import iac
from .agent import Agent
from .platform import Platform

STATIC = Path(__file__).parent / "static"
PLAN_TTL = 900


class Actor(BaseModel):
    name: str


class FaultIn(BaseModel):
    type: str = Field(pattern="^(link_down|link_up|crc_errors|cpu_spike|power_off|power_on|err_disable)$")
    device: str
    interface: str = ""


class ProposalIn(BaseModel):
    device: str
    interface: str
    action: str = Field(pattern="^(enable|disable)$")
    reason: str = Field(min_length=3)


class DesiredIn(BaseModel):
    yaml: str


class ApplyIn(BaseModel):
    plan_id: str


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def create_app(platform: Platform | None = None, token: str | None = None) -> FastAPI:
    p = platform or Platform()
    s = p.store
    token = token if token is not None else os.environ.get("FATHOM_TOKEN", "")
    agent = Agent(p)
    plans: dict[str, tuple[float, iac.Plan]] = {}
    app = FastAPI(title="Fathom", version="0.1.0", description="AIOps platform for networks")

    def writer(authorization: str = Header(default=""), x_fathom_user: str = Header(default="")) -> Actor:
        if token and not secrets.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "missing or invalid token")
        return Actor(name=x_fathom_user.strip() or "anonymous")

    def need(row, what: str):
        if row is None:
            raise HTTPException(404, f"{what} not found")
        return row

    @app.get("/api/summary")
    def summary():
        return {
            "devices": s.row("SELECT COUNT(*) n, SUM(reachable) up FROM devices"),
            "incidents_open": s.row("SELECT COUNT(*) n FROM incidents WHERE status='open'")["n"],
            "alerts_firing": s.row("SELECT COUNT(*) n FROM alerts WHERE status='firing'")["n"],
            "jobs_pending": s.row("SELECT COUNT(*) n FROM jobs WHERE status='pending_approval'")["n"],
            "cycles": p.cycles, "sim_time": p.lab.clock, "agent": "llm" if agent.online else "offline",
        }

    @app.get("/api/devices")
    def devices():
        return s.rows("""SELECT d.*, (SELECT COUNT(*) FROM interfaces i WHERE i.device=d.name AND i.oper_up=0) ports_down,
                         (SELECT MAX(util) FROM interfaces i WHERE i.device=d.name) max_util
                         FROM devices d ORDER BY role, name""")

    @app.get("/api/devices/{name}")
    def device(name: str):
        d = need(s.row("SELECT * FROM devices WHERE name=?", (name,)), "device")
        d["interfaces"] = s.rows("SELECT * FROM interfaces WHERE device=? ORDER BY name", (name,))
        d["events"] = s.rows("SELECT * FROM events WHERE device=? ORDER BY ts DESC LIMIT 20", (name,))
        return d

    @app.get("/api/topology")
    def topology():
        nodes = s.rows("SELECT name, role, vendor, reachable FROM devices")
        seen, links = set(), []
        state = {(r["device"], r["name"]): r for r in s.rows("SELECT device, name, oper_up, util FROM interfaces")}
        reach = {n["name"]: n["reachable"] for n in nodes}
        for l in s.rows("SELECT a, a_if, b, b_if FROM links"):
            key = tuple(sorted([(l["a"], l["a_if"]), (l["b"], l["b_if"])]))
            if key in seen:
                continue
            seen.add(key)
            ends = [state.get((d, i)) for d, i in key if reach.get(d)]
            up = bool(ends) and all(e and e["oper_up"] for e in ends)
            util = max((e["util"] or 0 for e in ends if e), default=0)
            links.append({**l, "up": up, "util": util})
        return {"nodes": nodes, "links": links}

    @app.get("/api/metrics")
    def metrics(device: str, metric: str, target: str = "", minutes: int = Query(60, ge=1, le=10080)):
        since = p.lab.clock - minutes * 60
        return s.rows("SELECT ts, value FROM metrics WHERE device=? AND metric=? AND target=? AND ts>=? ORDER BY ts",
                      (device, metric, target, since))

    @app.get("/api/incidents")
    def incidents(status: str = "open"):
        return s.rows("""SELECT i.*, (SELECT COUNT(*) FROM alerts a WHERE a.incident_id=i.id) alert_count
                         FROM incidents i WHERE status=? ORDER BY id DESC LIMIT 100""", (status,))

    @app.get("/api/incidents/{incident_id}")
    def incident(incident_id: int):
        return need(agent.call("get_incident", {"incident_id": incident_id}), "incident") \
            if s.row("SELECT 1 FROM incidents WHERE id=?", (incident_id,)) else need(None, "incident")

    @app.get("/api/alerts")
    def alerts(status: str = "firing"):
        return s.rows("SELECT * FROM alerts WHERE status=? ORDER BY id DESC LIMIT 200", (status,))

    @app.get("/api/events")
    def events(limit: int = Query(50, ge=1, le=500)):
        return s.rows("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))

    @app.get("/api/jobs")
    def jobs(status: str | None = None):
        if status:
            return s.rows("SELECT * FROM jobs WHERE status=? ORDER BY id DESC LIMIT 100", (status,))
        return s.rows("SELECT * FROM jobs ORDER BY id DESC LIMIT 100")

    @app.post("/api/jobs")
    def propose(body: ProposalIn, who: Actor = Depends(writer)):
        try:
            jid = p.remediator.propose(f"port_{body.action}", body.device, body.interface, body.reason, who.name)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'"))
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return s.row("SELECT * FROM jobs WHERE id=?", (jid,))

    def decide(job_id: int, who: Actor, approve: bool):
        try:
            with p.lock:
                job = (p.remediator.approve if approve else p.remediator.reject)(job_id, who.name)
                if approve:
                    p.remediator.run_approved()
        except KeyError:
            raise HTTPException(404, "job not found")
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return s.row("SELECT * FROM jobs WHERE id=?", (job["id"],))

    @app.post("/api/jobs/{job_id}/approve")
    def approve(job_id: int, who: Actor = Depends(writer)):
        return decide(job_id, who, True)

    @app.post("/api/jobs/{job_id}/reject")
    def reject(job_id: int, who: Actor = Depends(writer)):
        return decide(job_id, who, False)

    @app.post("/api/iac/plan")
    def plan(body: DesiredIn, who: Actor = Depends(writer)):
        try:
            desired = iac.parse_desired(yaml.safe_load(body.yaml) or {})
            with p.lock:
                result = iac.plan(s, p.transport, desired)
        except (yaml.YAMLError, iac.PlanError) as exc:
            raise HTTPException(422, str(exc))
        now = time.time()
        for k, (ts, _) in list(plans.items()):
            if now - ts > PLAN_TTL:
                del plans[k]
        plan_id = secrets.token_hex(6)
        plans[plan_id] = (now, result)
        s.audit(who.name, "config.plan", "", {"plan_id": plan_id, "changes": result.change_count})
        return {"plan_id": plan_id, "expires_in": PLAN_TTL, **result.to_dict()}

    @app.post("/api/iac/apply")
    def apply(body: ApplyIn, who: Actor = Depends(writer)):
        entry = plans.pop(body.plan_id, None)
        if entry is None or time.time() - entry[0] > PLAN_TTL:
            raise HTTPException(410, "plan not found or expired; run plan again")
        with p.lock:
            return {"results": iac.apply(s, p.transport, entry[1], actor=who.name)}

    @app.get("/api/kb/search")
    def kb_search(q: str = Query(min_length=1)):
        return [{"doc": h.section.doc, "title": h.section.title, "section": h.section.heading,
                 "text": h.section.text, "score": h.score} for h in p.kb.search(q, limit=5)]

    @app.post("/api/agent/ask")
    def ask(body: AskIn):
        ans = agent.ask(body.question)
        return {"answer": ans.text, "mode": ans.mode,
                "steps": [{"tool": st.tool, "args": st.args} for st in ans.steps]}

    @app.post("/api/lab/faults")
    def inject(body: FaultIn, who: Actor = Depends(writer)):
        lab = p.lab
        if body.device not in lab.devices:
            raise HTTPException(404, "unknown device")
        needs_if = body.type in ("link_down", "link_up", "crc_errors", "err_disable")
        if needs_if and body.interface not in lab.devices[body.device].interfaces:
            raise HTTPException(404, "unknown interface")
        action = {
            "link_down": lambda: lab.fail_link(body.device, body.interface),
            "link_up": lambda: lab.restore_link(body.device, body.interface),
            "crc_errors": lambda: lab.corrupt_port(body.device, body.interface),
            "err_disable": lambda: lab.err_disable(body.device, body.interface),
            "cpu_spike": lambda: lab.cpu_spike(body.device),
            "power_off": lambda: lab.power(body.device, False),
            "power_on": lambda: lab.power(body.device, True),
        }[body.type]
        action()
        s.audit(who.name, f"lab.{body.type}", body.device, body.interface)
        return {"ok": True}

    @app.post("/api/lab/heal")
    def heal(who: Actor = Depends(writer)):
        p.lab.heal()
        s.audit(who.name, "lab.heal")
        return {"ok": True}

    @app.post("/api/cycle")
    def cycle(who: Actor = Depends(writer)):
        return p.cycle()

    @app.get("/api/audit")
    def audit(limit: int = Query(50, ge=1, le=500)):
        return s.rows("SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (limit,))

    @app.get("/healthz")
    def health():
        return {"status": "ok", "cycles": p.cycles}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.state.platform = p
    return app
