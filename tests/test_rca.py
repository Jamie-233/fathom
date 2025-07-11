from fathom.ai.rca import IncidentManager, analyze
from fathom.observe.rules import RuleEngine


def run(env, cycles=1):
    eng = RuleEngine(env.store, clock=env.clock)
    mgr = IncidentManager(env.store, clock=env.clock)
    for _ in range(cycles):
        env.step()
        eng.evaluate()
        mgr.sync()
    return eng, mgr


def by_key(store):
    return {f.key: f for f in analyze(store)}


def test_dead_distribution_switch_is_one_incident(env):
    env.lab.power("dist-01", False)
    run(env)
    findings = by_key(env.store)
    assert list(findings) == ["device:dist-01"]
    f = findings["device:dist-01"]
    assert f.confidence == "high"
    assert sorted(f.impact) == ["acc-01", "acc-02"]
    # dist-01 itself + 2 isolated access switches + 2 neighbour links facing it
    assert len(f.alerts) == 5 and len(f.root_alerts) == 1
    assert env.store.row("SELECT COUNT(*) n FROM incidents WHERE status='open'")["n"] == 1


def test_cut_access_uplink_is_reported_honestly_ambiguous(env):
    env.lab.fail_link("dist-02", "Gi1/0/2")
    run(env)
    [f] = analyze(env.store)
    assert f.kind == "link" and (f.device, f.target) == ("dist-02", "Gi1/0/2")
    assert f.confidence == "medium" and "indistinguishable" in f.title
    assert f.impact == ["acc-04"]


def test_redundant_link_failure_is_its_own_incident(env):
    env.lab.fail_link("dist-01", "Te1/0/2")
    run(env)
    [f] = analyze(env.store)
    assert f.kind == "link" and "redundancy lost" in f.title
    assert env.store.row("SELECT COUNT(*) n FROM alerts WHERE status='firing'")["n"] == 2  # both ends visible


def test_local_problems_are_separate_roots(env):
    env.lab.corrupt_port("acc-01", "Gi1/0/2", 5)
    env.lab.power("acc-04", False)
    run(env, cycles=2)
    kinds = {f.key.split(":")[0] for f in analyze(env.store)}
    assert kinds == {"local", "link"}  # single-homed acc-04: link-or-device


def test_incident_closes_when_fault_heals(env):
    env.lab.power("dist-02", False)
    run(env)
    env.lab.heal()
    run(env)
    inc = env.store.rows("SELECT status, closed FROM incidents")
    assert len(inc) == 1 and inc[0]["status"] == "closed" and inc[0]["closed"]


def test_incident_survives_restart_of_the_manager(env):
    env.lab.power("dist-02", False)
    run(env)
    run(env)  # new RuleEngine/IncidentManager instances, same store
    assert env.store.row("SELECT COUNT(*) n FROM incidents")["n"] == 1


def test_one_sided_lldp_still_yields_an_undirected_graph(env):
    # The lab records every link from both ends. Delete one direction: the
    # reverse edge must be synthesized, or a half-discovered link silently
    # vanishes from path search and device_dead().
    from fathom.ai.rca import Topology

    env.store.execute("DELETE FROM links WHERE a='dist-01' AND b='acc-01'")
    topo = Topology(env.store)
    assert ("Gi1/0/1", "acc-01", "Gi1/0/48") in topo.adj["dist-01"]
    assert ("Gi1/0/48", "dist-01", "Gi1/0/1") in topo.adj["acc-01"]


def test_half_discovered_link_still_resolves_to_a_link_finding(env):
    env.store.execute("DELETE FROM links WHERE a='dist-01' AND b='acc-01'")
    env.lab.power("acc-01", False)
    run(env)
    [f] = analyze(env.store)
    assert f.kind == "link" and f.confidence == "medium"
