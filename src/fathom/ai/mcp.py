"""MCP server: Fathom's operations tools inside Cursor, Claude and other MCP clients.

The server is a thin client of the REST API rather than a second copy of the
platform. That matters for safety as much as for architecture: every call
goes through the same token check, the same read-only allow-list, the same
four-eyes approval for changes, and lands in the same audit trail as a call
from the portal. An IDE plugin gets no privileges a person at the portal
wouldn't have.

Transport is MCP's stdio framing: one JSON-RPC 2.0 message per line.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, TextIO

import httpx

from fathom import __version__

PROTOCOL_VERSION = "2025-06-18"


def _string(desc: str, enum: list[str] | None = None) -> dict:
    out: dict[str, Any] = {"type": "string", "description": desc}
    if enum:
        out["enum"] = enum
    return out


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOLS: list[dict] = [
    {"name": "list_incidents", "description": "Open (or closed) incidents, each already grouped by root cause.",
     "inputSchema": _schema({"status": _string("open or closed", ["open", "closed"])}, [])},
    {"name": "get_incident", "description": "One incident with root vs. symptom alerts, recent logs and jobs.",
     "inputSchema": _schema({"incident_id": {"type": "integer"}}, ["incident_id"])},
    {"name": "device_status", "description": "Device reachability, CPU, uptime, interfaces and recent logs.",
     "inputSchema": _schema({"device": _string("Device name")}, ["device"])},
    {"name": "show_command", "description": "Run an allow-listed read-only command on a device.",
     "inputSchema": _schema({"device": _string("Device name"), "command": _string("e.g. show interfaces")},
                            ["device", "command"])},
    {"name": "verify_network", "description": "Live checks: reachability, links, err-disabled ports.",
     "inputSchema": _schema({}, [])},
    {"name": "search_runbooks", "description": "Search the team's runbooks.",
     "inputSchema": _schema({"query": _string("Keywords")}, ["query"])},
    {"name": "propose_port_change",
     "description": "Queue enabling/disabling a user port. Requires approval by a different person.",
     "inputSchema": _schema({"device": _string("Device"), "interface": _string("Interface"),
                             "action": _string("enable or disable", ["enable", "disable"]),
                             "reason": _string("Why, citing evidence")},
                            ["device", "interface", "action", "reason"])},
]


class ToolError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")


def _checked(r) -> Any:
    # Checked by status code rather than raise_for_status(): HTTP clients
    # differ in which exception class they raise, and the API's own "detail"
    # message is what the model needs to see.
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise ToolError(r.status_code, str(detail))
    return r.json()


class FathomMCP:
    def __init__(self, http: httpx.Client, user: str = "mcp"):
        self.http = http
        self.user = user
        self.calls: dict[str, Callable[[dict], Any]] = {
            "list_incidents": lambda a: self._get("/api/incidents", params={"status": a.get("status", "open")}),
            "get_incident": lambda a: self._get(f"/api/incidents/{int(a['incident_id'])}"),
            "device_status": lambda a: self._get(f"/api/devices/{a['device']}"),
            "show_command": lambda a: self._get(f"/api/devices/{a['device']}/show", params={"command": a["command"]}),
            "verify_network": lambda a: self._get("/api/verify"),
            "search_runbooks": lambda a: self._get("/api/kb/search", params={"q": a["query"]}),
            "propose_port_change": lambda a: self._post("/api/jobs", {
                "device": a["device"], "interface": a["interface"], "action": a["action"], "reason": a["reason"]}),
        }

    def _get(self, path: str, **kw) -> Any:
        return _checked(self.http.get(path, **kw))

    def _post(self, path: str, body: dict) -> Any:
        return _checked(self.http.post(path, json=body, headers={"X-Fathom-User": self.user}))

    def handle(self, msg: dict) -> dict | None:
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:  # notification (e.g. notifications/initialized): no reply
            return None
        try:
            if method == "initialize":
                result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "fathom", "version": __version__}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._call(msg.get("params") or {})
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        except Exception as exc:  # protocol-level failure
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _call(self, params: dict) -> dict:
        name, args = params.get("name"), params.get("arguments") or {}
        fn = self.calls.get(name)
        if fn is None:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        try:
            data = fn(args)
        except ToolError as exc:
            # Tool errors go back as results, not protocol errors, so the
            # model sees "403: cannot approve your own job" and can adapt.
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except (KeyError, ValueError) as exc:
            return {"content": [{"type": "text", "text": f"bad arguments: {exc}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}],
                "isError": False}

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                reply = self.handle(msg)
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


def main() -> None:
    base = os.environ.get("FATHOM_URL", "http://127.0.0.1:8000")
    token = os.environ.get("FATHOM_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=base, headers=headers, timeout=30) as http:
        FathomMCP(http, user=os.environ.get("FATHOM_USER", "mcp")).serve()
