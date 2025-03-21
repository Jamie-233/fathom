import pytest

from fathom.collector import Collector
from fathom.lab import Lab, LabTransport
from fathom.store import Store


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Env:
    """A lab, a store and a collector sharing one fake clock."""

    def __init__(self, seed=11):
        self.lab = Lab(seed=seed)
        self.store = Store()
        self.clock = Clock()
        self.transport = LabTransport(self.lab)
        self.collector = Collector(self.store, self.transport, clock=self.clock)
        self.collector.register([vars(d) for d in self.lab.devices.values()])
        self.collector.discover()
        self.collector.poll_all()

    def step(self, seconds=30, cycles=1):
        for _ in range(cycles):
            self.lab.tick(seconds)
            self.clock.t += seconds
            self.collector.poll_all()


@pytest.fixture
def env():
    return Env()
