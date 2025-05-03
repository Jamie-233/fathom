"""Prometheus exposition and Alertmanager-compatible notifications.

Fathom keeps its own store for incidents and RCA, but the metrics it
collects are exported in the Prometheus text format, so an existing
Prometheus/Grafana stack can scrape them and build dashboards without
running a second poller against the same devices.

Rates are exported as gauges of what Fathom computed, plus the raw error
total as a counter. Exporting only pre-computed rates would stop Prometheus
from doing its own ``rate()`` over other windows; exporting only counters
would hide the wrap/reset handling Fathom already did correctly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fathom.data.store import Store


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _lbl(**kv) -> str:
    if not kv:
        return ""
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in kv.items()) + "}"


def exposition(store: Store) -> str:
    out: list[str] = []

    def family(name: str, kind: str, help_text: str, rows: list[tuple[dict, float]]) -> None:
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")
        out.extend(f"{name}{_lbl(**labels)} {value}" for labels, value in rows)

    devices = store.rows("SELECT name, role, vendor, reachable, cpu, uptime FROM devices ORDER BY name")
    family("fathom_device_up", "gauge", "1 if the device answers the poller.",
           [({"device": d["name"], "role": d["role"], "vendor": d["vendor"]}, d["reachable"]) for d in devices])
    family("fathom_device_cpu_percent", "gauge", "Control-plane CPU.",
           [({"device": d["name"]}, d["cpu"]) for d in devices if d["cpu"] is not None])
    family("fathom_device_uptime_seconds", "gauge", "Uptime reported by the device.",
           [({"device": d["name"]}, d["uptime"]) for d in devices if d["uptime"] is not None])

    ifs = store.rows("SELECT * FROM interfaces ORDER BY device, name")
    lab = lambda i: {"device": i["device"], "interface": i["name"]}  # noqa: E731
    family("fathom_interface_oper_up", "gauge", "Operational state.", [(lab(i), i["oper_up"]) for i in ifs])
    family("fathom_interface_utilisation_percent", "gauge", "Max of in/out utilisation.",
           [(lab(i), i["util"]) for i in ifs if i["util"] is not None])
    family("fathom_interface_in_bits_per_second", "gauge", "Inbound rate.",
           [(lab(i), round(i["in_bps"], 1)) for i in ifs if i["in_bps"] is not None])
    family("fathom_interface_crc_errors_total", "counter", "CRC errors since the device counter started.",
           [(lab(i), i["crc_total"]) for i in ifs if i["crc_total"] is not None])

    counts = store.rows("SELECT severity, COUNT(*) n FROM alerts WHERE status='firing' GROUP BY severity")
    family("fathom_alerts_firing", "gauge", "Firing alerts by severity.", [({"severity": c["severity"]}, c["n"]) for c in counts])
    open_inc = store.row("SELECT COUNT(*) n FROM incidents WHERE status='open'")["n"]
    family("fathom_incidents_open", "gauge", "Open incidents after root cause grouping.", [({}, open_inc)])
    return "\n".join(out) + "\n"


def _iso(ts: float | None) -> str:
    # Alertmanager uses the zero time for "not ended yet".
    if not ts:
        return "0001-01-01T00:00:00Z"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def alertmanager_payload(store: Store, incident_id: int, status: str, external_url: str = "") -> dict:
    """Shape an incident like an Alertmanager webhook, so existing receivers
    (chat bridges, ticketing, on-call tools) accept it unchanged. The group is
    the incident, the alerts are its members, and the root is labelled."""
    inc = store.row("SELECT * FROM incidents WHERE id=?", (incident_id,))
    alerts = store.rows("SELECT * FROM alerts WHERE incident_id=?", (incident_id,))
    return {
        "version": "4",
        "status": status,
        "receiver": "fathom",
        "groupKey": f"incident:{incident_id}",
        "groupLabels": {"incident": str(incident_id)},
        "commonLabels": {"incident": str(incident_id), "root_device": inc["root_device"] or ""},
        "commonAnnotations": {"summary": inc["title"], "confidence": inc["confidence"] or ""},
        "externalURL": external_url,
        "alerts": [{
            "status": "firing" if a["status"] == "firing" else "resolved",
            "labels": {"alertname": a["rule"], "severity": a["severity"], "device": a["device"],
                       "interface": a["target"] or "", "role": "root" if a["root"] else "symptom"},
            "annotations": {"summary": a["summary"]},
            "startsAt": _iso(a["started"]), "endsAt": _iso(a["ended"]),
            "fingerprint": a["fingerprint"],
        } for a in alerts],
    }
