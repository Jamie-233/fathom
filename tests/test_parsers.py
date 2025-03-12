import pytest

from fathom.lab import Lab
from fathom.parsers import (ParseError, canonical_ifname, parse_cpu, parse_ios_interfaces, parse_lldp,
                            parse_syslog, parse_uptime_seconds, parse_vrp_brief)


@pytest.fixture
def lab():
    lab = Lab(seed=1)
    lab.tick(5)
    return lab


def test_canonical_names_unify_long_and_short_forms():
    assert canonical_ifname("GigabitEthernet1/0/48") == "Gi1/0/48"
    assert canonical_ifname("TenGigabitEthernet1/0/1") == "Te1/0/1"
    assert canonical_ifname("GE0/0/1") == "GE0/0/1"


def test_ios_interfaces_from_lab_output(lab):
    stats = {s.name: s for s in parse_ios_interfaces(lab.execute("acc-01", "show interfaces"))}
    up = stats["Gi1/0/48"]
    assert up.oper_up and up.admin_up and up.description == "to-dist-01"
    assert up.in_bytes == lab.devices["acc-01"].interfaces["Gi1/0/48"].in_octets
    assert up.speed_mbps == 1000


def test_ios_admin_down_and_errors(lab):
    lab.configure("acc-01", ["interface Gi1/0/3", "shutdown"])
    lab.corrupt_port("acc-01", "Gi1/0/1", 100)
    lab.tick(10)
    stats = {s.name: s for s in parse_ios_interfaces(lab.execute("acc-01", "show interfaces"))}
    assert not stats["Gi1/0/3"].admin_up and not stats["Gi1/0/3"].oper_up
    assert stats["Gi1/0/1"].crc > 0 and stats["Gi1/0/1"].in_errors == stats["Gi1/0/1"].crc


def test_vrp_brief_merges_counters_and_star_means_admin_down(lab):
    lab.configure("acc-03", ["interface GE0/0/2", "shutdown"])
    stats = {s.name: s for s in parse_vrp_brief(lab.execute("acc-03", "display interface brief"),
                                                 lab.execute("acc-03", "display interface counters"))}
    assert not stats["GE0/0/2"].admin_up
    assert stats["GE0/0/48"].in_bytes == lab.devices["acc-03"].interfaces["GE0/0/48"].in_octets


def test_vrp_without_counters_reports_unknown_bytes_not_zero(lab):
    stats = parse_vrp_brief(lab.execute("acc-03", "display interface brief"))
    assert all(s.in_bytes is None for s in stats)


def test_format_change_raises_instead_of_returning_nothing():
    with pytest.raises(ParseError):
        parse_ios_interfaces("Interface table not available\n")
    with pytest.raises(ParseError):
        parse_vrp_brief("Interface PHY Protocol\nGE0/0/1 something odd\n")


def test_cpu_uptime_lldp(lab):
    assert 0 <= parse_cpu(lab.execute("core-01", "display cpu-usage")) <= 100
    assert parse_uptime_seconds("x uptime is 2 weeks, 3 days, 4 hours, 5 minutes") == 2 * 604800 + 3 * 86400 + 4 * 3600 + 300
    assert ("Gi1/0/48", "dist-01", "Gi1/0/1") in parse_lldp(lab.execute("acc-01", "show lldp neighbors"))


def test_syslog_both_vendors(lab):
    lab.fail_link("dist-02", "Gi1/0/1")
    lab.fail_link("core-01", "GE1/0/2")
    ios = [parse_syslog("dist-02", line) for line in lab.drain_syslog("dist-02")]
    vrp = [parse_syslog("core-01", line) for line in lab.drain_syslog("core-01")]
    link = next(e for e in ios if e.facility == "LINK")
    assert (link.interface, link.state, link.kind) == ("Gi1/0/1", "down", "link_state")
    assert any(e.facility == "LINEPROTO" and e.state is None for e in ios)
    assert (vrp[0].interface, vrp[0].state, vrp[0].severity) == ("GE1/0/2", "down", 2)


def test_isolated_device_buffers_syslog_until_path_returns(lab):
    lab.fail_link("dist-02", "Gi1/0/1")
    assert lab.drain_syslog("acc-03") == []
    lab.restore_link("dist-02", "Gi1/0/1")
    assert any("GE0/0/48" in line for line in lab.drain_syslog("acc-03"))


def test_unparseable_log_is_ignored():
    assert parse_syslog("x", "random noise") is None
