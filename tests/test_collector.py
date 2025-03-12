import pytest

from fathom.collector import Collector, counter_delta
from fathom.lab import Lab, LabTransport
from fathom.store import Store


def test_counter_delta_handles_wrap_and_reset():
    assert counter_delta(100, 250) == 150
    assert counter_delta(2**64 - 10, 5) == 15          # wrapped
    assert counter_delta(10_000_000, 1_200) is None     # reset, not a 2^64 spike
    assert counter_delta(2**32 - 1, 3, bits=32) == 4


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    lab = Lab(seed=3)
    store = Store()
    clock = Clock()
    col = Collector(store, LabTransport(lab), clock=clock)
    col.register([vars(d) for d in lab.devices.values()])
    return lab, store, col, clock


def step(lab, clock, col, seconds=30):
    lab.tick(seconds)
    clock.t += seconds
    return col.poll_all()


def test_rates_need_two_samples_then_match_simulated_load(env):
    lab, store, col, clock = env
    col.poll_all()
    assert store.row("SELECT util FROM interfaces WHERE device='acc-01' AND name='Gi1/0/48'")["util"] is None
    step(lab, clock, col)
    row = store.row("SELECT util, in_bps FROM interfaces WHERE device='acc-01' AND name='Gi1/0/48'")
    assert 0 < row["util"] < 100 and row["in_bps"] > 0


def test_vrp_devices_get_rates_too(env):
    lab, store, col, clock = env
    col.poll_all()
    step(lab, clock, col)
    assert store.row("SELECT util FROM interfaces WHERE device='acc-03' AND name='GE0/0/48'")["util"] > 0


def test_error_rate_per_minute(env):
    lab, store, col, clock = env
    col.poll_all()
    lab.corrupt_port("acc-02", "Gi1/0/2", errors_per_sec=10)
    step(lab, clock, col, 60)
    rate = store.row("SELECT err_per_min FROM interfaces WHERE device='acc-02' AND name='Gi1/0/2'")["err_per_min"]
    assert 350 < rate < 850


def test_unreachable_device_is_marked_and_keeps_last_known_state(env):
    lab, store, col, clock = env
    col.poll_all()
    lab.power("acc-04", False)
    results = {r.device: r for r in step(lab, clock, col)}
    assert not results["acc-04"].reachable
    assert store.row("SELECT reachable FROM devices WHERE name='acc-04'")["reachable"] == 0
    assert store.row("SELECT COUNT(*) n FROM interfaces WHERE device='acc-04'")["n"] > 0


def test_link_failure_produces_state_and_log_events(env):
    lab, store, col, clock = env
    col.poll_all()
    lab.fail_link("dist-01", "Gi1/0/2")
    step(lab, clock, col)
    assert store.row("SELECT oper_up FROM interfaces WHERE device='dist-01' AND name='Gi1/0/2'")["oper_up"] == 0
    ev = store.rows("SELECT device, interface, state FROM events WHERE kind='link_state'")
    assert {"device": "dist-01", "interface": "Gi1/0/2", "state": "down"} in ev
    # The far end is cut off from the poller, so it is unreachable, not "down".
    assert store.row("SELECT reachable FROM devices WHERE name='acc-02'")["reachable"] == 0


def test_lldp_discovery_finds_every_link(env):
    lab, store, col, _ = env
    assert col.discover() == 2 * len(lab.links)
