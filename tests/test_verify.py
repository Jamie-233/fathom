from fathom.observe.verify import checks, compare, snapshot


def by_name(results):
    return {c.name: c for c in results}


def test_healthy_network_passes_all_checks(env):
    assert all(c.passed for c in checks(snapshot(env.store, env.transport)))


def test_regression_is_detected(env):
    before = snapshot(env.store, env.transport)
    env.lab.fail_link("dist-02", "Gi1/0/2")
    after = snapshot(env.store, env.transport)
    r = by_name(compare(before, after))
    assert not r["no_devices_lost"].passed and "acc-04" in r["no_devices_lost"].detail
    assert not r["no_links_lost"].passed


def test_deliberate_shutdown_is_not_a_regression(env):
    before = snapshot(env.store, env.transport)
    env.lab.configure("acc-01", ["interface Gi1/0/5", "shutdown"])
    assert all(c.passed for c in compare(before, snapshot(env.store, env.transport)))


def test_pre_existing_fault_does_not_fail_the_comparison(env):
    env.lab.fail_link("core-01", "GE1/0/2")
    before = snapshot(env.store, env.transport)
    env.lab.configure("acc-02", ["interface Gi1/0/1", "description new"])
    after = snapshot(env.store, env.transport)
    assert all(c.passed for c in compare(before, after))
    assert not by_name(checks(after))["all_links_up"].passed


def test_crc_growth_and_err_disable(env):
    before = snapshot(env.store, env.transport)
    env.lab.corrupt_port("acc-03", "GE0/0/1", 50)
    env.lab.err_disable("acc-02", "Gi1/0/3")
    env.lab.tick(10)
    after = snapshot(env.store, env.transport)
    assert not by_name(compare(before, after))["no_crc_growth"].passed
    assert "acc-02 Gi1/0/3" in by_name(checks(after))["no_err_disabled_ports"].detail
