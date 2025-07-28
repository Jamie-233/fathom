"""Keeps the benchmark harness honest: scoring rules and a small end-to-end run."""

import importlib.util
import random
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "rca_bench", Path(__file__).resolve().parent.parent / "benchmarks" / "rca_bench.py")
bench = importlib.util.module_from_spec(SPEC)
sys.modules["rca_bench"] = bench  # dataclasses look the module up by name
SPEC.loader.exec_module(bench)


def inc(id_, key, conf="high", title=""):
    return {"id": id_, "root_key": key, "confidence": conf, "title": title}


def test_exact_requires_the_right_kind_and_confidence():
    f = bench.Fault("power", {"dist-01"}, "device")
    assert bench.score_match(f, inc(1, "device:dist-01", "high")) == "exact"
    # right place, but the analyser said "link" or hedged: located, not exact
    assert bench.score_match(f, inc(1, "link:core-01:XGE1/0/3:dist-01:Te1/0/1", "medium")) == "located"
    assert bench.score_match(f, inc(1, "device:dist-01", "low")) == "located"
    assert bench.score_match(f, inc(1, "device:acc-01", "high")) is None


def test_single_homed_isolation_is_only_exact_when_it_admits_the_ambiguity():
    f = bench.Fault("power", {"acc-04"}, "link_isolating")
    assert bench.score_match(f, inc(1, "link:dist-02:Gi1/0/2:acc-04:GE0/0/48", "medium")) == "exact"
    # claiming a dead device with high confidence is overconfident: right place, wrong claim
    assert bench.score_match(f, inc(1, "device:acc-04", "high")) == "located"


def test_local_faults_need_the_matching_rule():
    f = bench.Fault("cpu", {"acc-01"}, "local:high_cpu")
    assert bench.score_match(f, inc(1, "local:high_cpu:acc-01:")) == "exact"
    assert bench.score_match(f, inc(1, "local:interface_errors:acc-01:Gi1/0/2")) is None


def test_matching_is_one_to_one_and_reports_leftovers():
    faults = [bench.Fault("cpu", {"a"}, "local:high_cpu"), bench.Fault("cpu", {"a"}, "local:high_cpu")]
    outcomes, extra = bench.match_all(faults, [inc(1, "local:high_cpu:a:"), inc(2, "device:zzz", "low")])
    assert outcomes == ["exact", None]
    assert [i["id"] for i in extra] == [2]


def test_baselines_are_expected_values_not_insertion_order():
    alerts = [{"device": d} for d in ("acc-01", "acc-02", "dist-01", "core-01", "dist-02")]
    acc = bench.baseline_accuracy(alerts, {"dist-01"})
    assert acc["random_alert"] == pytest.approx(0.2)
    assert acc["busiest_device"] == pytest.approx(0.2)  # five-way tie
    assert bench.baseline_accuracy([], {"x"}) == {"random_alert": 0.0, "busiest_device": 0.0}


def test_wilson_interval_brackets_the_estimate():
    lo, hi = bench.wilson(95, 100)
    assert lo < 0.95 < hi and 0 <= lo and hi <= 1
    assert bench.wilson(0, 0) == (0.0, 0.0)


def test_random_topologies_are_valid_and_reproducible():
    spec = bench.random_topology(random.Random("x"))
    assert spec == bench.random_topology(random.Random("x"))
    names = [d["name"] for d in spec["devices"]]
    assert len(names) == len(set(names))
    used = [(a, ai) for a, ai, b, bi in spec["links"]] + [(b, bi) for a, ai, b, bi in spec["links"]]
    assert len(used) == len(set(used)), "an interface is wired twice"


@pytest.mark.parametrize("scenario", list(bench.SCENARIOS))
def test_every_scenario_runs_cleanly_without_noise(scenario):
    t = bench.run_trial(scenario, f"test:{scenario}", "lab", 0.0)
    assert not t.skipped
    assert t.recovered, "incidents must close after the network heals"
    if scenario == "healthy_control":
        assert t.false_positives == 0
    else:
        assert all(o is not None for o in t.outcomes), t.notes


def test_trials_are_reproducible():
    a = bench.run_trial("two_faults", "same-seed", "random", 0.02)
    b = bench.run_trial("two_faults", "same-seed", "random", 0.02)
    assert (a.outcomes, a.false_positives, a.incidents, a.alerts) == (b.outcomes, b.false_positives, b.incidents, b.alerts)
