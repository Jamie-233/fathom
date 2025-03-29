import json

import pytest

from fathom.remediation import Remediator
from fathom.rules import RuleEngine


def cycle(env, eng, rem):
    env.step()
    opened = eng.evaluate()["opened"]
    jobs = rem.on_alerts(opened)
    rem.run_approved()
    return jobs


@pytest.fixture
def parts(env):
    return RuleEngine(env.store, clock=env.clock), Remediator(env.store, env.transport, clock=env.clock)


def test_err_disabled_port_is_recovered_automatically_and_verified(env, parts):
    eng, rem = parts
    env.lab.err_disable("acc-03", "GE0/0/5")
    [jid] = cycle(env, eng, rem)
    job = env.store.row("SELECT * FROM jobs WHERE id=?", (jid,))
    assert job["status"] == "succeeded" and job["approved_by"] == "policy"
    assert "undo shutdown" in json.loads(job["plan"])["commands"]
    assert env.lab.devices["acc-03"].interfaces["GE0/0/5"].oper_up
    assert "verification" in job["output"]


def test_repeated_err_disable_escalates_to_a_human(env, parts):
    eng, rem = parts
    statuses = []
    for _ in range(3):
        env.lab.err_disable("acc-01", "Gi1/0/5")
        jobs = cycle(env, eng, rem)
        statuses += [env.store.row("SELECT status FROM jobs WHERE id=?", (j,))["status"] for j in jobs]
        env.step()
        eng.evaluate()
    assert statuses == ["succeeded", "succeeded", "pending_approval"]
    assert json.loads(env.store.row("SELECT plan FROM jobs ORDER BY id DESC")["plan"])["escalated"]


def test_noisy_user_port_waits_for_approval_then_runs(env, parts):
    eng, rem = parts
    env.lab.corrupt_port("acc-02", "Gi1/0/3", 5)
    cycle(env, eng, rem)
    [jid] = cycle(env, eng, rem)
    assert env.store.row("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "pending_approval"
    assert env.lab.devices["acc-02"].interfaces["Gi1/0/3"].admin_up

    rem.approve(jid, "alice")
    rem.run_approved()
    job = env.store.row("SELECT * FROM jobs WHERE id=?", (jid,))
    assert job["status"] == "succeeded" and job["approved_by"] == "alice"
    assert not env.lab.devices["acc-02"].interfaces["Gi1/0/3"].admin_up
    assert env.store.row("SELECT actor FROM audit WHERE action='job.succeeded'")["actor"] == "alice"


def test_errors_on_an_uplink_never_create_a_shutdown_job(env, parts):
    eng, rem = parts
    env.lab.corrupt_port("dist-01", "Te1/0/1", 5)
    for _ in range(3):
        cycle(env, eng, rem)
    assert env.store.row("SELECT COUNT(*) n FROM jobs")["n"] == 0


def test_cpu_alert_collects_read_only_diagnostics(env, parts):
    eng, rem = parts
    env.lab.cpu_spike("core-01")
    jobs = []
    for _ in range(6):  # CPU ramps over a few polls before the 2-cycle rule fires
        jobs += cycle(env, eng, rem)
    [jid] = jobs
    job = env.store.row("SELECT * FROM jobs WHERE id=?", (jid,))
    assert job["status"] == "succeeded" and "display cpu-usage" in job["output"]


def test_decisions_only_apply_to_pending_jobs(env, parts):
    eng, rem = parts
    env.lab.corrupt_port("acc-02", "Gi1/0/3", 5)
    cycle(env, eng, rem)
    [jid] = cycle(env, eng, rem)
    rem.reject(jid, "bob")
    with pytest.raises(ValueError, match="rejected"):
        rem.approve(jid, "alice")
    assert rem.run_approved() == []


def test_unreachable_device_fails_the_job_cleanly(env, parts):
    eng, rem = parts
    env.lab.corrupt_port("acc-02", "Gi1/0/3", 5)
    cycle(env, eng, rem)
    [jid] = cycle(env, eng, rem)
    rem.approve(jid, "alice")
    env.lab.power("acc-02", False)
    rem.run_approved()
    job = env.store.row("SELECT * FROM jobs WHERE id=?", (jid,))
    assert job["status"] == "failed" and "DeviceUnreachable" in job["output"]
