from fathom.config.backup import backup_all, diff, history, normalise, restore_plan
from fathom.delivery.iac import apply


def test_only_changed_configs_are_stored(env):
    first = backup_all(env.store, env.transport, clock=env.clock)
    assert set(first.values()) == {"initial"}
    assert set(backup_all(env.store, env.transport, clock=env.clock).values()) == {"unchanged"}
    env.lab.configure("acc-02", ["interface Gi1/0/4", "description camera-lobby"])
    third = backup_all(env.store, env.transport, clock=env.clock)
    assert third["acc-02"] == "changed" and list(third.values()).count("unchanged") == 8


def test_diff_shows_the_change(env):
    backup_all(env.store, env.transport)
    env.lab.configure("acc-03", ["interface GE0/0/2", "shutdown"])
    backup_all(env.store, env.transport)
    new, old = history(env.store, "acc-03")
    text = diff(env.store, old["id"], new["id"])
    assert "+ shutdown" in text and "acc-03@" in text


def test_restore_reverts_only_what_changed(env):
    backup_all(env.store, env.transport)
    good = history(env.store, "acc-01")[0]["id"]
    env.lab.configure("acc-01", ["interface Gi1/0/1", "description WRONG", "interface Gi1/0/2", "shutdown"])
    plan = restore_plan(env.store, env.transport, good)
    assert plan.change_count == 2
    assert all(r["ok"] for r in apply(env.store, env.transport, plan))
    assert env.lab.devices["acc-01"].interfaces["Gi1/0/2"].admin_up
    assert env.lab.devices["acc-01"].interfaces["Gi1/0/1"].description == "user-port-1"


def test_unreachable_devices_are_reported_not_skipped_silently(env):
    env.lab.power("acc-04", False)
    assert backup_all(env.store, env.transport)["acc-04"] == "unreachable"


def test_volatile_lines_do_not_count_as_changes():
    a = "! Last configuration change at 10:00\nhostname x\n"
    b = "! Last configuration change at 11:00\nhostname x\n"
    assert normalise(a) == normalise(b)
