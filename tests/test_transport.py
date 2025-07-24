"""The SSH transport, exercised without a switch.

Netmiko is replaced by a fake module: the point is the behaviour the rest of
the platform depends on — command dispatch, vendor mapping, session reuse,
reconnect after a dropped session, and the syslog queue an external receiver
fills.
"""

import sys
import types

import pytest

from fathom.devices.lab import DEFAULT_TOPOLOGY, DeviceUnreachable, Lab
from fathom.devices.transport import LabTransport, SSHTransport, build_transport, load_inventory


class FakeConnection:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.commands = []
        self.saved = 0
        self.fail_next = False

    def send_command(self, command):
        if self.fail_next:
            self.fail_next = False
            raise OSError("connection reset by peer")
        self.commands.append(command)
        return f"output of {command}"

    def send_config_set(self, lines):
        self.commands.append(("config", tuple(lines)))
        return "config applied"

    def save_config(self):
        self.saved += 1
        return "saved"


@pytest.fixture
def fake_netmiko(monkeypatch):
    created = []

    class NetmikoTimeoutException(Exception):
        pass

    class NetmikoAuthenticationException(Exception):
        pass

    def connect(**kwargs):
        conn = FakeConnection(**kwargs)
        created.append(conn)
        return conn

    module = types.ModuleType("netmiko")
    module.ConnectHandler = connect
    exceptions = types.ModuleType("netmiko.exceptions")
    exceptions.NetmikoTimeoutException = NetmikoTimeoutException
    exceptions.NetmikoAuthenticationException = NetmikoAuthenticationException
    module.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "netmiko", module)
    monkeypatch.setitem(sys.modules, "netmiko.exceptions", exceptions)
    monkeypatch.setenv("FATHOM_SSH_USER", "netops")
    monkeypatch.setenv("FATHOM_SSH_PASSWORD", "s3cret")
    return types.SimpleNamespace(created=created, timeout=NetmikoTimeoutException,
                                 auth=NetmikoAuthenticationException)


INVENTORY = {"sw1": {"vendor": "cisco_ios", "mgmt": "10.0.0.1"}}


def test_run_caches_the_session(fake_netmiko):
    t = SSHTransport(INVENTORY)
    assert t.run("sw1", "show version") == "output of show version"
    t.run("sw1", "show clock")
    assert len(fake_netmiko.created) == 1


def test_vendor_maps_to_the_right_netmiko_driver(fake_netmiko):
    t = SSHTransport({
        "ios": {"vendor": "cisco_ios", "mgmt": "10.0.0.1"},
        "vrp": {"vendor": "huawei_vrp", "mgmt": "10.0.0.2"},
    })
    t.run("ios", "show version")
    t.run("vrp", "display version")
    drivers = {c.kwargs["host"]: c.kwargs["device_type"] for c in fake_netmiko.created}
    assert drivers == {"10.0.0.1": "cisco_ios", "10.0.0.2": "huawei"}


def test_a_dropped_session_is_retried_on_a_fresh_connection(fake_netmiko):
    t = SSHTransport(INVENTORY)
    t.run("sw1", "show version")
    fake_netmiko.created[0].fail_next = True
    with pytest.raises(DeviceUnreachable):
        t.run("sw1", "show version")
    assert t.run("sw1", "show version") == "output of show version"
    assert len(fake_netmiko.created) == 2


def test_configure_pushes_lines_and_saves(fake_netmiko):
    t = SSHTransport(INVENTORY)
    out = t.configure("sw1", ["interface Gi1/0/1", "shutdown"])
    assert "config applied" in out and "saved" in out
    assert fake_netmiko.created[0].saved == 1


def test_missing_credentials_are_reported_clearly(fake_netmiko, monkeypatch):
    monkeypatch.delenv("FATHOM_SSH_USER")
    t = SSHTransport(INVENTORY)
    with pytest.raises(RuntimeError, match="FATHOM_SSH_USER"):
        t.run("sw1", "show version")


def test_timeout_becomes_unreachable(fake_netmiko, monkeypatch):
    def refuse(**kwargs):
        raise fake_netmiko.timeout("timed out after 60s")

    monkeypatch.setattr(sys.modules["netmiko"], "ConnectHandler", refuse)
    t = SSHTransport(INVENTORY)
    with pytest.raises(DeviceUnreachable, match="timed out"):
        t.run("sw1", "show version")


def test_syslog_arrives_from_an_external_receiver(fake_netmiko):
    t = SSHTransport(INVENTORY)
    t.push_syslog("sw1", "%LINK-3-UPDOWN: Gi1/0/1 changed state to down")
    assert t.syslog("sw1") == ["%LINK-3-UPDOWN: Gi1/0/1 changed state to down"]
    assert t.syslog("sw1") == []


def test_topology_file_doubles_as_the_ssh_inventory(fake_netmiko):
    inventory = load_inventory(DEFAULT_TOPOLOGY)
    assert inventory["core-01"] == {"vendor": "huawei_vrp", "mgmt": "10.0.0.2", "model": "S6730"}
    assert len(inventory) == 9


def test_build_transport_picks_the_implementation(fake_netmiko):
    from fathom.devices.lab import Lab as LabClass

    lab = LabClass()
    assert isinstance(build_transport("lab", lab, DEFAULT_TOPOLOGY), LabTransport)
    assert isinstance(build_transport("ssh", lab, DEFAULT_TOPOLOGY), SSHTransport)
    with pytest.raises(ValueError, match="unknown transport"):
        build_transport("telnet", lab, DEFAULT_TOPOLOGY)


def test_platform_still_defaults_to_the_lab():
    from fathom.platform import Platform

    p = Platform(seed=1)
    assert isinstance(p.transport, LabTransport)
    assert isinstance(Platform(seed=1, transport="lab").transport, LabTransport)
