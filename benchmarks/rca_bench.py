"""Fault-injection benchmark for Fathom's root cause analysis.

What this measures
------------------
Each trial builds a fresh simulated network, lets it settle, injects one or
more faults, runs a few evaluation cycles through the real collector, rule
engine and incident manager, and then compares the *open incidents* with the
faults that were injected.

What this does NOT measure
--------------------------
The simulator and the RCA were written by the same author, so a high score
shows the logic is internally consistent, not that it works on real networks.
Three things keep the numbers from being trivially perfect:

* seeds, fault targets and (optionally) topologies are randomised per trial;
* a flaky poller drops polls at random, as a real management path does;
* two naive baselines are scored on the same trials.

Ground truth comes from the lab (which devices are really unreachable), never
from the RCA's own output.

Usage::

    python benchmarks/rca_bench.py --trials 100 --seed 1 --out docs/benchmark.md
"""

from __future__ import annotations

import argparse
import json
import math
import platform as pyplatform
import random
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from fathom.ai.rca import IncidentManager
from fathom.devices.lab import DEFAULT_TOPOLOGY, DeviceUnreachable, Lab
from fathom.devices.transport import LabTransport
from fathom.data.store import Store
from fathom.observe.collector import Collector
from fathom.observe.rules import RuleEngine, default_rules

STEP = 30.0
WARMUP_CYCLES = 3
OBSERVE_CYCLES = 10
RECOVER_CYCLES = 3


# --------------------------------------------------------------------------
# Topologies
# --------------------------------------------------------------------------

def random_topology(rng: random.Random) -> dict:
    """A campus with 2-4 distribution switches, mixed vendors and a few
    dual-homed access switches. Same device roles and port naming as lab.yml
    so the vendor renderers and parsers see familiar text."""
    n_dist = rng.choice([2, 3, 4])
    devices = [
        {"name": "edge-01", "vendor": "cisco_ios", "role": "edge", "mgmt": "10.0.0.1", "model": "ISR4331"},
        {"name": "core-01", "vendor": "huawei_vrp", "role": "core", "mgmt": "10.0.0.2", "model": "S6730"},
        {"name": "core-02", "vendor": "huawei_vrp", "role": "core", "mgmt": "10.0.0.3", "model": "S6730"},
    ]
    links = [
        ["edge-01", "Gi0/0/1", "core-01", "GE1/0/1"],
        ["edge-01", "Gi0/0/2", "core-02", "GE1/0/1"],
        ["core-01", "GE1/0/2", "core-02", "GE1/0/2"],
    ]
    core_port = {"core-01": 3, "core-02": 3}
    dist_next_port = {}
    dists = []
    for i in range(1, n_dist + 1):
        name = f"dist-{i:02d}"
        dists.append(name)
        devices.append({"name": name, "vendor": "cisco_ios", "role": "distribution",
                        "mgmt": f"10.0.0.{10 + i}", "model": "C9500"})
        dist_next_port[name] = 1
        core = "core-01" if i % 2 else "core-02"
        links.append([core, f"XGE1/0/{core_port[core]}", name, "Te1/0/1"])
        core_port[core] += 1
    peers = {}
    for a, b in zip(dists[0::2], dists[1::2]):
        links.append([a, "Te1/0/2", b, "Te1/0/2"])
        peers[a], peers[b] = b, a
    n_acc = 0
    for d in dists:
        for _ in range(rng.choice([1, 2, 3])):
            n_acc += 1
            name = f"acc-{n_acc:02d}"
            vendor = rng.choice(["cisco_ios", "huawei_vrp"])
            up1, up2 = ("Gi1/0/48", "Gi1/0/47") if vendor == "cisco_ios" else ("GE0/0/48", "GE0/0/47")
            devices.append({"name": name, "vendor": vendor, "role": "access",
                            "mgmt": f"10.0.1.{n_acc}", "model": "C9200" if vendor == "cisco_ios" else "S5735"})
            links.append([d, f"Gi1/0/{dist_next_port[d]}", name, up1])
            dist_next_port[d] += 1
            if d in peers and rng.random() < 0.3:
                p = peers[d]
                links.append([p, f"Gi1/0/{dist_next_port[p]}", name, up2])
                dist_next_port[p] += 1
    return {"site": "rand", "devices": devices, "links": links, "access_ports": 6}


def write_topology(spec: dict) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False, encoding="utf-8")
    yaml.safe_dump(spec, f)
    f.close()
    return Path(f.name)


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

class FlakyTransport:
    """Drops whole polls of a device at random: the management path is not
    perfect, and a lost poll looks exactly like an unreachable device."""

    def __init__(self, inner: LabTransport, rng: random.Random, p: float):
        self.inner, self.rng, self.p = inner, rng, p
        self.down: set[str] = set()

    def new_cycle(self, names) -> None:
        self.down = {n for n in names if self.p and self.rng.random() < self.p}

    def run(self, device: str, command: str) -> str:
        if device in self.down:
            raise DeviceUnreachable(f"{device}: poll lost")
        return self.inner.run(device, command)

    def syslog(self, device: str) -> list[str]:
        return [] if device in self.down else self.inner.syslog(device)

    def configure(self, device: str, lines: list[str]) -> str:
        return self.inner.configure(device, lines)


class Env:
    def __init__(self, topology: Path, lab_seed: int, noise: float, rng: random.Random, unreachable_cycles: int = 1):
        self.lab = Lab(topology, seed=lab_seed)
        self.store = Store()
        self.t = 1_000_000.0
        clock = lambda: self.t  # noqa: E731
        self.flaky = FlakyTransport(LabTransport(self.lab), rng, noise)
        self.collector = Collector(self.store, self.flaky, clock=clock)
        self.collector.register([vars(d) for d in self.lab.devices.values()])
        self.collector.discover()
        rules = default_rules()
        for r in rules:
            if r.name == "device_unreachable":
                r.for_cycles = unreachable_cycles
        self.rules = RuleEngine(self.store, rules=rules, clock=clock)
        self.mgr = IncidentManager(self.store, clock=clock)
        self.opened: list[int] = []
        self.step()  # first poll primes the counters

    def step(self) -> None:
        self.lab.tick(STEP)
        self.t += STEP
        self.flaky.new_cycle(self.lab.devices)
        self.collector.poll_all()
        self.rules.evaluate()
        self.opened += self.mgr.sync()["opened"]

    def run(self, cycles: int) -> None:
        for _ in range(cycles):
            self.step()

    def open_incidents(self) -> list[dict]:
        return self.store.rows("SELECT * FROM incidents WHERE status='open' ORDER BY id")

    def firing_alerts(self) -> list[dict]:
        return self.store.rows("SELECT id, rule, device, target FROM alerts WHERE status='firing' ORDER BY id")


# --------------------------------------------------------------------------
# Ground truth and scoring
# --------------------------------------------------------------------------

@dataclass
class Fault:
    cls: str                       # scenario class, for reporting
    locs: set[str]                 # devices that count as "the right place"
    expect: str                    # device | link_isolating | link_redundant | local:<rule> | any_structural
    detail: str = ""


def incident_view(inc: dict) -> tuple[str, set[str], str, str]:
    key = inc["root_key"]
    typ = key.split(":")[0]
    if typ == "device":
        return typ, {key.split(":")[1]}, "", inc["confidence"]
    if typ == "link":
        _, a, _ai, b, _bi = key.split(":")
        return typ, {a, b}, inc["title"], inc["confidence"]
    return typ, {key.split(":")[2]}, key.split(":")[1], inc["confidence"]


def score_match(fault: Fault, inc: dict) -> str | None:
    typ, locs, extra, conf = incident_view(inc)
    if fault.expect.startswith("local:"):
        ok = typ == "local" and extra == fault.expect.split(":", 1)[1] and bool(locs & fault.locs)
        return "exact" if ok else None
    if not locs & fault.locs:
        return None
    if fault.expect == "device":
        return "exact" if typ == "device" and conf == "high" else "located"
    if fault.expect == "link_isolating":
        return "exact" if typ == "link" and conf == "medium" else "located"
    if fault.expect == "link_redundant":
        return "exact" if typ == "link" and "redundancy lost" in extra and conf == "high" else "located"
    return "located"


def match_all(faults: list[Fault], incidents: list[dict]) -> tuple[list[str | None], list[dict]]:
    """Greedy one-to-one matching, exact matches first."""
    result: list[str | None] = [None] * len(faults)
    used: set[int] = set()
    for want in ("exact", "located"):
        for i, f in enumerate(faults):
            if result[i]:
                continue
            for inc in incidents:
                if inc["id"] in used:
                    continue
                if score_match(f, inc) == want:
                    result[i] = want
                    used.add(inc["id"])
                    break
    return result, [inc for inc in incidents if inc["id"] not in used]


def baseline_accuracy(alerts: list[dict], truth: set[str]) -> dict[str, float]:
    """Expected accuracy of two naive pickers that only see alerts.

    Both choose uniformly at random where the alerts leave a choice, so the
    result is a probability rather than an accident of insertion order:
    ``random_alert`` pages the device of one alert chosen at random;
    ``busiest_device`` takes the device with the most alerts, ties at random.
    """
    if not alerts:
        return {"random_alert": 0.0, "busiest_device": 0.0}
    random_alert = sum(a["device"] in truth for a in alerts) / len(alerts)
    counts = Counter(a["device"] for a in alerts)
    top = max(counts.values())
    tied = [d for d, c in counts.items() if c == top]
    return {"random_alert": random_alert, "busiest_device": sum(d in truth for d in tied) / len(tied)}


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

def _links(lab: Lab) -> list[tuple[str, str, str, str]]:
    return list(lab.links)


def _isolated(lab: Lab) -> list[str]:
    return [n for n, d in lab.devices.items() if d.powered and not lab.reachable(n)]


def _visible_neighbours(lab: Lab, dev: str) -> int:
    """Neighbours the poller can still see once ``dev`` is gone. A switch whose
    only visible neighbour is its uplink cannot be told apart from a cut cable."""
    return sum(1 for i in lab.devices[dev].interfaces.values() if i.peer and lab.reachable(i.peer[0]))


def _user_port(lab: Lab, rng: random.Random, dev: str) -> str:
    ports = [n for n, i in lab.devices[dev].interfaces.items() if i.peer is None]
    return rng.choice(sorted(ports))


def _reachable_access(lab: Lab, rng: random.Random, exclude: set[str] = frozenset()) -> str | None:
    c = [n for n, d in lab.devices.items() if d.role == "access" and d.powered and lab.reachable(n) and n not in exclude]
    return rng.choice(sorted(c)) if c else None


def inject_power(lab: Lab, rng: random.Random, roles: tuple[str, ...]) -> Fault | None:
    cands = sorted(n for n, d in lab.devices.items() if d.role in roles)
    if not cands:
        return None
    dev = rng.choice(cands)
    lab.power(dev, False)
    expect = "device" if _visible_neighbours(lab, dev) > 1 else "link_isolating"
    return Fault("power", {dev}, expect, f"power off {dev}")


def inject_link(lab: Lab, rng: random.Random, want_isolating: bool) -> Fault | None:
    cands = []
    for a, ai, b, bi in _links(lab):
        lab.fail_link(a, ai)
        iso = bool(_isolated(lab))
        lab.restore_link(a, ai)
        if iso == want_isolating:
            cands.append((a, ai, b, bi))
    if not cands:
        return None
    a, ai, b, bi = rng.choice(cands)
    lab.fail_link(a, ai)
    return Fault("link", {a, b}, "link_isolating" if want_isolating else "link_redundant", f"cut {a} {ai} <-> {b} {bi}")


def inject_double_link(lab: Lab, rng: random.Random) -> list[Fault] | None:
    """Cut every infrastructure uplink of a distribution switch.

    Whether that isolates it depends on the topology (a dual-homed access
    switch can still carry the path), so ground truth is read from the lab
    afterwards: an isolation if something went dark, otherwise two separate
    redundancy-lost links."""
    cands = []
    for d, dev in lab.devices.items():
        if dev.role != "distribution":
            continue
        ups = [(n, i) for n, i in dev.interfaces.items() if i.peer and lab.devices[i.peer[0]].role in ("core", "distribution")]
        if len(ups) >= 2:
            cands.append((d, ups))
    if not cands:
        return None
    d, ups = rng.choice(cands)
    for n, _ in ups:
        lab.fail_link(d, n)
    if d in _isolated(lab):
        return [Fault("double_link", {d}, "link_isolating", f"cut all uplinks of {d} (isolated)")]
    return [Fault("double_link", {d, i.peer[0]}, "link_redundant", f"cut {d} {n}") for n, i in ups]


def inject_cpu(lab: Lab, rng: random.Random, exclude: set[str] = frozenset()) -> Fault | None:
    c = sorted(n for n, d in lab.devices.items() if d.powered and lab.reachable(n) and n not in exclude)
    if not c:
        return None
    dev = rng.choice(c)
    lab.cpu_spike(dev)
    return Fault("cpu", {dev}, "local:high_cpu", f"cpu spike on {dev}")


def inject_crc(lab: Lab, rng: random.Random, exclude: set[str] = frozenset()) -> Fault | None:
    dev = _reachable_access(lab, rng, exclude)
    if not dev:
        return None
    port = _user_port(lab, rng, dev)
    lab.corrupt_port(dev, port, rng.uniform(1.0, 40.0))
    return Fault("crc", {dev}, "local:interface_errors", f"CRC errors on {dev} {port}")


def inject_bpdu(lab: Lab, rng: random.Random, exclude: set[str] = frozenset()) -> Fault | None:
    dev = _reachable_access(lab, rng, exclude)
    if not dev:
        return None
    port = _user_port(lab, rng, dev)
    lab.err_disable(dev, port)
    return Fault("bpdu", {dev}, "local:port_err_disabled", f"BPDU guard on {dev} {port}")


def inject_multi(lab: Lab, rng: random.Random) -> list[Fault] | None:
    first = inject_power(lab, rng, ("distribution",))
    if not first:
        return None
    down = set(_isolated(lab)) | first.locs
    second = rng.choice([inject_cpu, inject_crc, inject_bpdu])(lab, rng, down)
    return [first, second] if second else None


SCENARIOS = {
    "power_distribution": lambda lab, rng: [inject_power(lab, rng, ("distribution",))],
    "power_core_edge": lambda lab, rng: [inject_power(lab, rng, ("core", "edge"))],
    "power_access": lambda lab, rng: [inject_power(lab, rng, ("access",))],
    "link_cut_isolating": lambda lab, rng: [inject_link(lab, rng, True)],
    "link_cut_redundant": lambda lab, rng: [inject_link(lab, rng, False)],
    "link_cut_all_uplinks": lambda lab, rng: inject_double_link(lab, rng),
    "cpu_spike": lambda lab, rng: [inject_cpu(lab, rng)],
    "crc_errors": lambda lab, rng: [inject_crc(lab, rng)],
    "bpdu_guard": lambda lab, rng: [inject_bpdu(lab, rng)],
    "two_faults": lambda lab, rng: inject_multi(lab, rng),
    "healthy_control": lambda lab, rng: [],
}


# --------------------------------------------------------------------------
# Trial
# --------------------------------------------------------------------------

@dataclass
class Trial:
    scenario: str
    n_faults: int
    outcomes: list[str | None]
    false_positives: int
    churn: int                       # incidents opened and later replaced/closed during the window
    incidents: int
    alerts: int
    recovered: bool
    baselines: dict[str, float] = field(default_factory=dict)
    detect_cycles: list[int | None] = field(default_factory=list)
    skipped: bool = False
    notes: list[str] = field(default_factory=list)


def run_trial(scenario: str, seed: str, topo_mode: str, noise: float, unreachable_cycles: int = 1) -> Trial:
    rng = random.Random(seed)
    tmp = None
    topology = DEFAULT_TOPOLOGY
    if topo_mode == "random":
        topology = tmp = write_topology(random_topology(rng))
    try:
        env = Env(topology, lab_seed=rng.randrange(10**6), noise=noise, rng=rng, unreachable_cycles=unreachable_cycles)
        env.run(WARMUP_CYCLES)
        pre_opened = len(env.opened)
        faults = SCENARIOS[scenario](env.lab, rng)
        if faults is None or any(f is None for f in faults):
            return Trial(scenario, 0, [], 0, 0, 0, 0, True, skipped=True)
        detect: list[int | None] = [None] * len(faults)
        cycles = OBSERVE_CYCLES if scenario != "healthy_control" else 20
        for c in range(1, cycles + 1):
            env.step()
            if faults:
                for i, o in enumerate(match_all(faults, env.open_incidents())[0]):
                    if o and detect[i] is None:
                        detect[i] = c
        incidents = env.open_incidents()
        alerts = env.firing_alerts()
        outcomes, extra = match_all(faults, incidents)
        # churn: incidents opened since injection that are not open any more
        churn = len(env.opened) - pre_opened - len(incidents)
        base: dict[str, float] = {}
        if len(faults) == 1:
            base = baseline_accuracy(alerts, faults[0].locs)
        # for the healthy control every incident ever opened is a false page
        fp = len(extra) if scenario != "healthy_control" else len(env.opened)
        notes = []
        if scenario != "healthy_control" and (any(o != "exact" for o in outcomes) or extra):
            seen = "; ".join(f"{i['root_key']} [{i['confidence']}]" for i in incidents) or "no incident"
            notes.append(f"`{seed}` — injected: {' + '.join(f.detail for f in faults)}; "
                         f"outcomes: {', '.join(o or 'missed' for o in outcomes)}; open incidents: {seen}")
        elif scenario == "healthy_control" and env.opened:
            notes.append(f"`{seed}` — {len(env.opened)} incident(s) opened with nothing wrong")
        env.lab.heal()
        env.run(RECOVER_CYCLES)
        recovered = not env.open_incidents()
        return Trial(scenario, len(faults), outcomes, fp, max(0, churn), len(incidents), len(alerts), recovered, base, detect, notes=notes)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d, (c + m) / d


def pct(k: int, n: int) -> str:
    if n == 0:
        return "-"
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.0f}% ({100 * lo:.0f}-{100 * hi:.0f})"


def summarise(trials: list[Trial]) -> dict:
    out = {}
    by = defaultdict(list)
    for t in trials:
        by[t.scenario].append(t)
    for name, ts in by.items():
        ran = [t for t in ts if not t.skipped]
        faults = sum(t.n_faults for t in ran)
        exact = sum(o == "exact" for t in ran for o in t.outcomes)
        located = sum(o in ("exact", "located") for t in ran for o in t.outcomes)
        missed = sum(o is None for t in ran for o in t.outcomes)
        n = len(ran)
        out[name] = {
            "trials": n, "skipped": len(ts) - n, "faults": faults,
            "exact": exact, "located": located, "missed": missed,
            "never": sum(c is None for t in ran for c in t.detect_cycles),
            "fp_total": sum(t.false_positives for t in ran),
            "trials_with_fp": sum(t.false_positives > 0 for t in ran),
            "churn_total": sum(t.churn for t in ran),
            "mean_incidents": sum(t.incidents for t in ran) / n if n else 0,
            "mean_alerts": sum(t.alerts for t in ran) / n if n else 0,
            "recovered": sum(t.recovered for t in ran),
            "baseline": {b: sum(t.baselines.get(b, 0.0) for t in ran) for b in ("random_alert", "busiest_device")},
            "detect": sorted(c for t in ran for c in t.detect_cycles if c is not None),
            "examples": [n for t in ran for n in t.notes][:3],
            "baseline_n": sum(1 for t in ran if t.baselines),
        }
    return out


def _detect(s: dict) -> str:
    d = s["detect"]
    if not d:
        return "-"
    med = d[len(d) // 2]
    return f"{med} / {d[-1]}"


def render_table(summary: dict) -> str:
    head = ("| Scenario | Trials | Faults | Exact (95% CI) | Located (95% CI) | Missed at end / never seen | "
            "Trials with a false incident | Alerts → incidents | Detect, median / max (cycles) | Closed after heal |\n"
            "|---|---:|---:|---|---|---:|---:|---|---:|---:|\n")
    rows = []
    for name in SCENARIOS:
        s = summary.get(name)
        if not s:
            continue
        if name == "healthy_control":
            rows.append(f"| {name} | {s['trials']} | 0 | - | - | - | {s['trials_with_fp']} / {s['trials']} "
                        f"({s['fp_total']} incidents in total) | - | - | - |")
            continue
        skipped = f" (+{s['skipped']} n/a)" if s["skipped"] else ""
        rows.append(
            f"| {name} | {s['trials']}{skipped} "
            f"| {s['faults']} | {pct(s['exact'], s['faults'])} | {pct(s['located'], s['faults'])} | {s['missed']} / {s['never']} "
            f"| {s['trials_with_fp']} / {s['trials']} | {s['mean_alerts']:.1f} → {s['mean_incidents']:.1f} "
            f"| {_detect(s)} | {s['recovered']} / {s['trials']} |")
    return head + "\n".join(rows) + "\n"


def render_baselines(summary: dict) -> str:
    head = ("| Scenario (single fault) | RCA located | Baseline: random alert | Baseline: busiest device |\n"
            "|---|---|---|---|\n")
    rows = []
    for name in SCENARIOS:
        s = summary.get(name)
        if not s or name == "healthy_control" or s["baseline_n"] == 0:
            continue
        n = s["baseline_n"]
        rows.append(f"| {name} | {pct(s['located'], s['faults'])} "
                    f"| {100 * s['baseline']['random_alert'] / n:.0f}% | {100 * s['baseline']['busiest_device'] / n:.0f}% |")
    return head + "\n".join(rows) + "\n"


def git_rev() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent,
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--trials", type=int, default=100)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--noise", type=float, nargs="+", default=[0.0, 0.02],
                    help="probability that a device's poll is lost in a given cycle")
    ap.add_argument("--topologies", nargs="+", default=["lab", "random"], choices=["lab", "random"])
    ap.add_argument("--scenarios", nargs="+", default=list(SCENARIOS), choices=list(SCENARIOS))
    ap.add_argument("--unreachable-cycles", type=int, default=1,
                    help="consecutive failed polls before a device_unreachable alert fires (default rule: 1)")
    ap.add_argument("--out", help="write a markdown report here")
    ap.add_argument("--json", help="write raw summaries here")
    args = ap.parse_args(argv)

    started = time.time()
    sections, raw = [], {}
    for topo_mode in args.topologies:
        for noise in args.noise:
            trials = []
            for sc in args.scenarios:
                for i in range(args.trials):
                    trials.append(run_trial(sc, f"{args.seed}:{topo_mode}:{noise}:{sc}:{i}", topo_mode, noise,
                                            args.unreachable_cycles))
            summary = summarise(trials)
            raw[f"{topo_mode}/noise={noise}"] = summary
            label = ("bundled 9-device lab" if topo_mode == "lab" else "randomly generated campus (2-4 distribution switches, 3-12 access switches)")
            sections.append((topo_mode, noise, label, summary))
            print(f"[{topo_mode} noise={noise}] done in {time.time() - started:.0f}s", file=sys.stderr)

    md = [f"# RCA benchmark\n",
          f"Generated by `benchmarks/rca_bench.py` — commit `{git_rev()}`, Python {pyplatform.python_version()}, "
          f"{args.trials} trials per scenario, base seed {args.seed}, "
          f"`device_unreachable` fires after {args.unreachable_cycles} failed poll(s).\n",
          "**This is a simulated lab.** The simulator and the analyser share an author, so these numbers show that the "
          "logic is consistent and where it breaks, not how it would perform on production switches. "
          "See the verification table in the README for what has and has not been exercised against real devices.\n",
          "## How to read the columns\n",
          "- **Exact**: the open incident points at the right place *and* has the right kind and confidence "
          "(a dead multi-homed switch → `device`/high; a cut single-homed uplink or a dead single-homed switch → "
          "`link`/medium, because those two look identical from the poller; a failed redundant link → `link`/high "
          "\"redundancy lost\"; CPU, CRC and BPDU faults → the matching local incident).",
          "- **Located**: the right place, but the kind or confidence differs from the expectation above "
          "(includes Exact).",
          "- **Missed at end / never seen**: injected faults with no incident pointing at them when the window ended, "
          "and those that never produced one at any cycle (the difference is incidents that opened and then closed again).",
          "- **Trials with a false incident**: trials that ended with an open incident that matches no injected fault.",
          "- **Alerts → incidents**: mean number of firing alerts versus incidents an on-call engineer would see.",
          "- Intervals are 95% Wilson confidence intervals over faults.\n",
          "Ground truth is taken from the lab (what is really unreachable), not from the RCA output. "
          "Fault targets, lab seeds and (in the random topology runs) the network itself change on every trial. "
          "`noise` is the probability that a device's poll is lost in a cycle.\n"]
    for topo_mode, noise, label, summary in sections:
        md.append(f"## {label}, poll loss {noise:.0%}\n")
        md.append(render_table(summary))
        md.append("\nSingle-fault trials versus alert-only baselines "
                  "(`random alert` = page the device of one firing alert picked at random; `busiest device` = device with the most "
                  "firing alerts, ties at random; both are expected values, not samples):\n")
        md.append(render_baselines(summary))
        ex = [(n, s_["examples"]) for n, s_ in summary.items() if s_["examples"]]
        if ex:
            md.append("\nCases that were not exact (up to three per scenario; the quoted seed reproduces the trial):\n")
            for n, items in ex:
                md.append(f"- **{n}**")
                md += [f"  - {i}" for i in items]
        md.append("")
    text = "\n".join(md)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(raw, indent=2), encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
