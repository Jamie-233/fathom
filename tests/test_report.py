from fastapi.testclient import TestClient

from fathom.api.app import create_app
from fathom.ops.report import build, to_html
from fathom.platform import Platform


def test_report_leads_with_verdict_and_lists_incidents():
    p = Platform(seed=12)
    p.cycle()
    assert "✅ Healthy" in build(p.store, p.transport, now=p.lab.clock)
    p.lab.power("dist-01", False)
    p.cycle()
    md = build(p.store, p.transport, now=p.lab.clock)
    assert "⚠️ Attention needed — 1 open incident(s)" in md
    assert "dist-01 is down" in md and "| all_devices_reachable | **FAIL** |" in md


def test_html_rendering_escapes_and_builds_tables():
    out = to_html("# T\n\n| a | b |\n|---|---|\n| <x> | **y** |\n\n- item\n")
    assert "<table>" in out and "<th>a</th>" in out and "&lt;x&gt;" in out
    assert "<strong>y</strong>" in out and "<li>item</li>" in out


def test_report_and_backup_endpoints():
    p = Platform(seed=12)
    p.cycle()
    c = TestClient(create_app(p, token="", allow_anonymous_writes=True))
    assert c.get("/api/report", params={"format": "html"}).text.startswith("<!doctype html>")
    assert set(c.post("/api/backups").json().values()) == {"initial"}
    p.lab.configure("acc-01", ["interface Gi1/0/2", "shutdown"])
    assert c.post("/api/backups").json()["acc-01"] == "changed"
    new, old = c.get("/api/backups/acc-01").json()
    assert "+ shutdown" in c.get(f"/api/backups/diff/{old['id']}/{new['id']}").text
