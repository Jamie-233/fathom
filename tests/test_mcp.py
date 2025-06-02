import io
import json

import pytest
from fastapi.testclient import TestClient

from fathom.ai.mcp import FathomMCP
from fathom.api.app import create_app
from fathom.platform import Platform


@pytest.fixture
def mcp():
    p = Platform(seed=13)
    p.lab.power("dist-02", False)
    p.cycle()
    client = TestClient(create_app(p, token="tok"), headers={"Authorization": "Bearer tok"})
    return p, FathomMCP(client, user="cursor")


def rpc(server, method, params=None, mid=1):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def call(server, name, **args):
    res = rpc(server, "tools/call", {"name": name, "arguments": args})["result"]
    return res["isError"], res["content"][0]["text"]


def test_handshake_and_tool_listing(mcp):
    _, s = mcp
    init = rpc(s, "initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert init["serverInfo"]["name"] == "fathom" and "tools" in init["capabilities"]
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in rpc(s, "tools/list")["result"]["tools"]}
    assert {"list_incidents", "propose_port_change", "show_command"} <= names


def test_read_tools_return_platform_data(mcp):
    _, s = mcp
    err, text = call(s, "list_incidents")
    assert not err and json.loads(text)[0]["title"] == "dist-02 is down"
    err, text = call(s, "show_command", device="acc-01", command="show interfaces")
    assert not err and "GigabitEthernet" in text


def test_write_attempts_are_gated_like_any_user(mcp):
    p, s = mcp
    err, text = call(s, "show_command", device="acc-01", command="reload")
    assert err and text.startswith("400")
    err, text = call(s, "propose_port_change", device="acc-01", interface="Gi1/0/2", action="disable",
                     reason="loop seen in logs")
    job = json.loads(text)
    assert not err and job["status"] == "pending_approval" and job["requested_by"] == "cursor"
    assert p.lab.devices["acc-01"].interfaces["Gi1/0/2"].admin_up


def test_stdio_framing_and_unknown_method(mcp):
    _, s = mcp
    out = io.StringIO()
    s.serve(io.StringIO('{"jsonrpc":"2.0","id":7,"method":"ping"}\nnot json\n'
                        '{"jsonrpc":"2.0","id":8,"method":"nope"}\n'), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0] == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert replies[1]["error"]["code"] == -32700
    assert replies[2]["error"]["code"] == -32601
