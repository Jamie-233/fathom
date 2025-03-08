from pathlib import Path

import pytest

from fathom.collector import Collector
from fathom.iac import PlanError, apply, load_desired, parse_desired, plan
from fathom.lab import Lab, LabTransport
from fathom.store import Store

EXAMPLE = Path(__file__).parent.parent / "examples" / "desired.yml"


@pytest.fixture
def env():
    lab = Lab(seed=5)
    store = Store()
    t = LabTransport(lab)
    Collector(store, t).register([vars(d) for d in lab.devices.values()])
    return lab, store, t


def test_plan_lists_only_declared_differences_with_vendor_commands(env):
    lab, store, t = env
    p = plan(store, t, load_desired(EXAMPLE))
    by = {d.device: d for d in p.devices}
    assert p.change_count == 7  # enabled: true on already-up ports is not a change
    assert "interface Gi1/0/6" in by["acc-01"].commands and " shutdown" in by["acc-01"].commands
    assert " description meeting-room-a" in by["acc-03"].commands


def test_apply_then_replan_is_empty(env):
    lab, store, t = env
    desired = load_desired(EXAMPLE)
    results = apply(store, t, plan(store, t, desired))
    assert all(r["ok"] for r in results)
    assert lab.devices["acc-03"].interfaces["GE0/0/6"].admin_up is False
    assert plan(store, t, desired).change_count == 0
    assert store.row("SELECT COUNT(*) n FROM audit WHERE action='config.apply'")["n"] == 2


def test_manual_change_shows_up_as_drift(env):
    lab, store, t = env
    desired = load_desired(EXAMPLE)
    apply(store, t, plan(store, t, desired))
    lab.configure("acc-01", ["interface Gi1/0/6", "no shutdown"])
    drift = plan(store, t, desired)
    assert drift.change_count == 1
    assert drift.devices[0].changes[0].field == "enabled"


def test_stale_plan_is_refused(env):
    lab, store, t = env
    p = plan(store, t, load_desired(EXAMPLE))
    lab.configure("acc-01", ["interface Gi1/0/5", "description changed-meanwhile"])
    results = {r["device"]: r for r in apply(store, t, p)}
    assert not results["acc-01"]["ok"] and "re-run plan" in results["acc-01"]["detail"]
    assert results["acc-03"]["ok"]


def test_unreachable_device_does_not_block_others(env):
    lab, store, t = env
    lab.power("acc-01", False)
    p = plan(store, t, load_desired(EXAMPLE))
    results = {r["device"]: r for r in apply(store, t, p)}
    assert not results["acc-01"]["ok"] and results["acc-03"]["ok"]


@pytest.mark.parametrize("doc, msg", [
    ({}, "devices"),
    ({"devices": {"acc-01": {"interfaces": {"Gi1/0/1": {"vlan": 10}}}}}, "unsupported"),
])
def test_invalid_desired_state(doc, msg):
    with pytest.raises(PlanError, match=msg):
        parse_desired(doc)


def test_unknown_interface_or_device(env):
    _, store, t = env
    with pytest.raises(PlanError, match="does not exist"):
        plan(store, t, {"acc-01": {"interfaces": {"Gi9/9/9": {"enabled": True}}}})
    with pytest.raises(PlanError, match="inventory"):
        plan(store, t, {"ghost": {}})
