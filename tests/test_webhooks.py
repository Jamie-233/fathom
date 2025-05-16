import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from fathom.api.app import create_app
from fathom.platform import Platform

SECRET = "s3cret"


@pytest.fixture
def ctx():
    p = Platform(seed=9)
    p.cycle()
    return p, TestClient(create_app(p, token="", netbox_secret=SECRET))


def send(client, event, secret=SECRET):
    body = json.dumps(event).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    return client.post("/api/webhooks/netbox", content=body,
                       headers={"X-Hook-Signature": sig, "Content-Type": "application/json"})


def iface_event(device, name, rid="r1", **fields):
    return {"event": "updated", "model": "interface", "request_id": rid,
            "data": {"name": name, "device": {"name": device}, **fields}}


def test_signed_change_is_applied_to_the_device(ctx):
    p, c = ctx
    r = send(c, iface_event("acc-01", "Gi1/0/2", description="finance-printer")).json()
    assert r["status"] == "applied" and r["regressions"] == []
    assert p.lab.devices["acc-01"].interfaces["Gi1/0/2"].description == "finance-printer"
    assert p.store.row("SELECT actor FROM audit WHERE action='config.apply'")["actor"] == "netbox"


def test_bad_signature_is_rejected(ctx):
    _, c = ctx
    assert send(c, iface_event("acc-01", "Gi1/0/2", description="x"), secret="wrong").status_code == 403


def test_retry_with_same_request_id_is_not_reapplied(ctx):
    _, c = ctx
    first = send(c, iface_event("acc-03", "GE0/0/2", rid="dup", enabled=False)).json()
    second = send(c, iface_event("acc-03", "GE0/0/2", rid="dup", enabled=False)).json()
    assert first["status"] == "applied" and second["duplicate"]


def test_echo_of_current_state_stops_the_loop(ctx):
    _, c = ctx
    send(c, iface_event("acc-02", "Gi1/0/1", rid="a", description="desk-12"))
    echo = send(c, iface_event("acc-02", "Gi1/0/1", rid="b", description="desk-12")).json()
    assert echo["status"] == "noop"


def test_uplinks_cannot_be_disabled_from_netbox(ctx):
    p, c = ctx
    r = send(c, iface_event("dist-01", "Gi1/0/1", enabled=False))
    assert r.status_code == 409 and p.lab.devices["dist-01"].interfaces["Gi1/0/1"].admin_up


def test_unhandled_and_invalid_events(ctx):
    _, c = ctx
    assert send(c, {"event": "deleted", "model": "device", "data": {}}).json()["status"] == "ignored"
    assert send(c, iface_event("acc-01", "Gi7/7/7", description="x")).status_code == 422
