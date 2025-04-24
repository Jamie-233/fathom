"""Wiring: one evaluation cycle, in order.

    tick lab → poll devices → evaluate rules → group into incidents
            → create/run remediation jobs → notify

The order is the contract. Incidents are built from this cycle's alerts, and
remediation only reacts to alerts that just opened, so a job is never created
twice for one alert and never from stale state. Everything runs on a single
thread per cycle; concurrency lives inside the stages that benefit from it
(SSH fan-out), not between stages, where it would only add ordering bugs.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from fathom.observe.collector import Collector
from fathom.ai.knowledge import KnowledgeBase
from fathom.devices.lab import DEFAULT_TOPOLOGY, Lab
from fathom.devices.transport import LabTransport
from fathom.ai.rca import IncidentManager
from fathom.ops.remediation import Remediator
from fathom.observe.rules import RuleEngine
from fathom.data.store import Store

log = logging.getLogger("fathom")


class Platform:
    def __init__(self, db: str = ":memory:", topology: str | Path = DEFAULT_TOPOLOGY,
                 webhooks: list[str] | None = None, seed: int | None = 7, sim_seconds: float = 30.0):
        self.lab = Lab(topology, seed=seed)
        self.store = Store(db)
        self.transport = LabTransport(self.lab)
        self.sim_seconds = sim_seconds
        # The poller runs on the lab's clock so simulated time and measured
        # rates agree even when the lab is fast-forwarded.
        clock = lambda: self.lab.clock  # noqa: E731
        self.collector = Collector(self.store, self.transport, clock=clock)
        self.rules = RuleEngine(self.store, clock=clock)
        self.incidents = IncidentManager(self.store, clock=clock)
        self.remediator = Remediator(self.store, self.transport, clock=clock)
        self.kb = KnowledgeBase()
        self.webhooks = webhooks or []
        self._notify_pool = ThreadPoolExecutor(max_workers=2)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cycles = 0
        self.lock = threading.Lock()

        self.collector.register([vars(d) for d in self.lab.devices.values()])
        self.collector.discover()
        self.collector.poll_all()

    def cycle(self) -> dict:
        with self.lock:
            self.lab.tick(self.sim_seconds)
            self.collector.poll_all()
            alerts = self.rules.evaluate()
            incidents = self.incidents.sync()
            jobs = self.remediator.on_alerts(alerts["opened"])
            ran = self.remediator.run_approved()
            self.cycles += 1
            if self.cycles % 20 == 0:
                self.collector.discover()
                self.store.prune(7 * 86400)
        for inc_id in incidents["opened"]:
            self._notify("incident.opened", inc_id)
        for inc_id in incidents["closed"]:
            self._notify("incident.closed", inc_id)
        return {"alerts": alerts, "incidents": incidents, "jobs_created": jobs, "jobs_run": ran}

    def _notify(self, event: str, incident_id: int) -> None:
        if not self.webhooks:
            return
        inc = self.store.row("SELECT * FROM incidents WHERE id=?", (incident_id,))
        payload = {"event": event, "incident": inc,
                   "text": f"[{event}] #{incident_id} {inc['title']}"}

        def send(url: str) -> None:
            # Off the cycle thread: a slow chat webhook must never delay polling.
            try:
                httpx.post(url, json=payload, timeout=5).raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("webhook %s failed: %s", url, exc)
                self.store.audit("fathom", "webhook.failed", "", {"url": url, "error": str(exc)})

        for url in self.webhooks:
            self._notify_pool.submit(send, url)

    def start(self, interval: float = 2.0) -> None:
        def loop():
            while not self._stop.wait(interval):
                try:
                    self.cycle()
                except Exception:  # keep the platform alive; the error is logged
                    log.exception("cycle failed")
        self._thread = threading.Thread(target=loop, name="fathom-cycle", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._notify_pool.shutdown(wait=False)

