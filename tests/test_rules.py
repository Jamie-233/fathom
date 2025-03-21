from fathom.rules import RuleEngine


def firing(store):
    return {(a["rule"], a["device"], a["target"]) for a in store.rows("SELECT * FROM alerts WHERE status='firing'")}


def test_healthy_network_raises_nothing(env):
    eng = RuleEngine(env.store, clock=env.clock)
    env.step()
    eng.evaluate()
    assert firing(env.store) == set()


def test_link_down_alerts_on_the_visible_side_only(env):
    eng = RuleEngine(env.store, clock=env.clock)
    env.lab.fail_link("dist-01", "Gi1/0/1")
    env.step()
    eng.evaluate()
    assert firing(env.store) == {("link_down", "dist-01", "Gi1/0/1"), ("device_unreachable", "acc-01", "")}


def test_user_port_going_down_is_not_an_alert(env):
    eng = RuleEngine(env.store, clock=env.clock)
    env.lab.configure("acc-01", ["interface Gi1/0/2", "shutdown"])
    env.lab.fail_link("acc-01", "Gi1/0/3")
    env.step()
    eng.evaluate()
    assert firing(env.store) == set()


def test_pending_period_then_fire_then_resolve_once(env):
    eng = RuleEngine(env.store, clock=env.clock)
    env.lab.corrupt_port("acc-02", "Gi1/0/2", 5)
    env.step()
    assert eng.evaluate()["opened"] == []          # 1st breach: pending
    env.step()
    opened = eng.evaluate()["opened"]               # 2nd: fires
    assert len(opened) == 1
    env.step()
    assert eng.evaluate()["opened"] == []           # still firing: no duplicate
    env.lab.corrupt_port("acc-02", "Gi1/0/2", 0)
    env.step()
    assert eng.evaluate()["resolved"] == opened


def test_breach_that_clears_during_pending_never_fires(env):
    eng = RuleEngine(env.store, clock=env.clock)
    env.lab.cpu_spike("core-02")
    env.step()
    eng.evaluate()
    env.lab.cpu_spike("core-02", False)
    env.step(cycles=3)
    eng.evaluate()
    assert env.store.row("SELECT COUNT(*) n FROM alerts")["n"] == 0


def test_err_disable_detected_for_both_vendors(env):
    eng = RuleEngine(env.store, clock=env.clock)
    env.lab.err_disable("acc-01", "Gi1/0/4")
    env.lab.err_disable("acc-04", "GE0/0/4")
    env.step()
    eng.evaluate()
    assert {("port_err_disabled", "acc-01", "Gi1/0/4"), ("port_err_disabled", "acc-04", "GE0/0/4")} <= firing(env.store)
    assert not any(t == "Gi1/0/48" for _, _, t in firing(env.store))
