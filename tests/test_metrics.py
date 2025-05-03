from fastapi.testclient import TestClient

from fathom.api.app import create_app
from fathom.observe.metrics import alertmanager_payload, exposition
from fathom.platform import Platform


def test_exposition_format_and_values():
    p = Platform(seed=8)
    p.cycle()
    text = exposition(p.store)
    assert "# TYPE fathom_interface_crc_errors_total counter" in text
    assert 'fathom_device_up{device="acc-01",role="access",vendor="cisco_ios"} 1' in text
    assert 'fathom_interface_utilisation_percent{device="acc-01",interface="Gi1/0/48"}' in text
    assert "fathom_incidents_open 0" in text


def test_alertmanager_payload_marks_root_and_symptoms():
    p = Platform(seed=8)
    p.lab.power("dist-02", False)
    res = p.cycle()
    [inc] = res["incidents"]["opened"]
    body = alertmanager_payload(p.store, inc, "firing")
    roles = sorted(a["labels"]["role"] for a in body["alerts"])
    assert roles.count("root") == 1 and len(roles) == 5
    assert body["groupKey"] == f"incident:{inc}" and body["commonAnnotations"]["summary"] == "dist-02 is down"
    assert all(a["endsAt"] == "0001-01-01T00:00:00Z" for a in body["alerts"])


def test_metrics_endpoint():
    p = Platform(seed=8)
    p.cycle()
    r = TestClient(create_app(p, token="")).get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "fathom_device_up" in r.text
