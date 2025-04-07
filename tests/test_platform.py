import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from fathom.platform import Platform


def test_full_cycle_from_fault_to_incident_to_auto_remediation():
    p = Platform(seed=2)
    p.cycle()
    p.lab.err_disable("acc-02", "Gi1/0/6")
    result = p.cycle()
    assert result["alerts"]["opened"] and result["incidents"]["opened"] and result["jobs_run"]
    assert p.lab.devices["acc-02"].interfaces["Gi1/0/6"].oper_up
    p.cycle()
    assert p.store.row("SELECT status FROM incidents")["status"] == "closed"


def test_incident_webhook_is_delivered():
    received = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    p = Platform(seed=2, webhooks=[f"http://127.0.0.1:{srv.server_address[1]}/"])
    p.lab.power("dist-02", False)
    p.cycle()
    p._notify_pool.shutdown(wait=True)
    srv.shutdown()
    assert received and received[0]["event"] == "incident.opened"
    assert "dist-02" in received[0]["text"]
