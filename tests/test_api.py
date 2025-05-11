from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fathom.api.app import create_app
from fathom.platform import Platform

DESIRED = (Path(__file__).parent.parent / "examples" / "desired.yml").read_text()


@pytest.fixture
def client():
    p = Platform(seed=6)
    p.cycle()
    return TestClient(create_app(p, token="t0ken"))


def auth(user="alice"):
    return {"Authorization": "Bearer t0ken", "X-Fathom-User": user}


def test_reads_are_open_writes_need_token(client):
    assert client.get("/api/summary").json()["devices"]["n"] == 9
    assert client.post("/api/lab/heal").status_code == 401
    assert client.post("/api/lab/heal", headers=auth()).status_code == 200


def test_fault_to_incident_through_the_api(client):
    client.post("/api/lab/faults", json={"type": "power_off", "device": "dist-01"}, headers=auth())
    client.post("/api/cycle", headers=auth())
    [inc] = client.get("/api/incidents").json()
    assert inc["title"] == "dist-01 is down" and inc["alert_count"] == 5
    detail = client.get(f"/api/incidents/{inc['id']}").json()
    assert sum(a["root"] for a in detail["alerts"]) == 1
    topo = client.get("/api/topology").json()
    assert {n["name"]: n["reachable"] for n in topo["nodes"]}["acc-01"] == 0
    assert any(not l["up"] for l in topo["links"])


def test_validation_and_not_found(client):
    assert client.post("/api/lab/faults", json={"type": "meteor", "device": "x"}, headers=auth()).status_code == 422
    assert client.post("/api/lab/faults", json={"type": "link_down", "device": "acc-01", "interface": "Gi9/9"},
                       headers=auth()).status_code == 404
    assert client.get("/api/incidents/999").status_code == 404
    assert client.get("/api/devices/nope").status_code == 404


def test_propose_approve_four_eyes(client):
    job = client.post("/api/jobs", json={"device": "acc-01", "interface": "Gi1/0/3", "action": "disable",
                                          "reason": "loop reported"}, headers=auth("alice")).json()
    assert client.post(f"/api/jobs/{job['id']}/approve", headers=auth("alice")).status_code == 403
    done = client.post(f"/api/jobs/{job['id']}/approve", headers=auth("bob")).json()
    assert done["status"] == "succeeded" and done["approved_by"] == "bob"
    assert client.post(f"/api/jobs/{job['id']}/approve", headers=auth("bob")).status_code == 409


def test_plan_then_apply_by_id_only_once(client):
    plan = client.post("/api/iac/plan", json={"yaml": DESIRED}, headers=auth()).json()
    assert plan["change_count"] == 7
    res = client.post("/api/iac/apply", json={"plan_id": plan["plan_id"]}, headers=auth()).json()
    assert all(r["ok"] for r in res["results"])
    assert res["regressions"] == [] and len(res["verification"]) == 4
    assert client.post("/api/iac/apply", json={"plan_id": plan["plan_id"]}, headers=auth()).status_code == 410
    again = client.post("/api/iac/plan", json={"yaml": DESIRED}, headers=auth()).json()
    assert again["change_count"] == 0


def test_bad_desired_state_is_422(client):
    r = client.post("/api/iac/plan", json={"yaml": "devices: {acc-01: {interfaces: {Gi1/0/1: {vlan: 3}}}}"},
                    headers=auth())
    assert r.status_code == 422 and "unsupported" in r.json()["detail"]


def test_agent_and_kb(client):
    assert client.get("/api/kb/search", params={"q": "bpdu"}).json()[0]["doc"] == "bpdu-guard"
    ans = client.post("/api/agent/ask", json={"question": "anything wrong?"}).json()
    assert ans["mode"] == "offline"


def test_metrics_and_portal(client):
    client.post("/api/cycle", headers=auth())
    pts = client.get("/api/metrics", params={"device": "acc-01", "metric": "util", "target": "Gi1/0/48"}).json()
    assert pts and pts[-1]["value"] > 0
    assert "Fathom" in client.get("/").text


def test_verify_endpoint(client):
    assert all(c["passed"] for c in client.get("/api/verify").json())
