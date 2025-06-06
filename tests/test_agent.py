import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from fathom.ai.agent import Agent
from fathom.platform import Platform


@pytest.fixture
def platform():
    p = Platform(seed=4)
    p.cycle()
    return p


def test_offline_explains_the_open_incident_with_runbook(platform):
    platform.lab.power("dist-01", False)
    platform.cycle()
    ans = Agent(platform).ask("what's broken?")
    assert ans.mode == "offline"
    assert "dist-01 is down" in ans.text and "acc-01" in ans.text
    assert "Device unreachable — Resolution" in ans.text


def test_offline_with_no_incidents(platform):
    assert "healthy" in Agent(platform).ask("status?").text


def test_show_command_is_allowlisted(platform):
    a = Agent(platform)
    assert "output" in a.call("show_command", {"device": "acc-01", "command": "show interfaces"})
    assert "error" in a.call("show_command", {"device": "acc-01", "command": "reload"})
    assert "error" in a.call("show_command", {"device": "acc-01", "command": "configure terminal"})


def test_agent_can_only_propose_never_apply(platform):
    a = Agent(platform)
    res = a.call("propose_port_change", {"device": "acc-03", "interface": "GE0/0/2", "action": "disable",
                                          "reason": "looping device"})
    assert res["status"] == "pending_approval"
    platform.cycle()
    assert platform.lab.devices["acc-03"].interfaces["GE0/0/2"].admin_up  # still up
    with pytest.raises(PermissionError):
        platform.remediator.approve(res["job_id"], "agent")


def test_llm_loop_against_fake_openai_server(platform):
    platform.lab.power("dist-02", False)
    platform.cycle()
    turns = []

    class Fake(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            turns.append(body)
            if len(turns) == 1:
                msg = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "list_incidents", "arguments": "{}"}}]}
            else:
                tool_result = json.loads(body["messages"][-1]["content"])
                msg = {"role": "assistant", "content": f"Root cause: {tool_result[0]['title']}"}
            data = json.dumps({"choices": [{"message": msg}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ans = Agent(platform, base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1", model="m").ask("why?")
    finally:
        srv.shutdown()
    assert ans.text == "Root cause: dist-02 is down"
    assert [s.tool for s in ans.steps] == ["list_incidents"]
    tool_props = turns[0]["tools"][0]["function"]["parameters"]["properties"]
    assert all("optional" not in v for v in tool_props.values())
    assert platform.store.row("SELECT actor FROM audit WHERE action='tool.list_incidents'")["actor"] == "agent"
