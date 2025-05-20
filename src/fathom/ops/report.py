"""Inspection report (巡检报告).

A daily report is read by people who were not on call, so it leads with the
verdict and only then the evidence: is anything broken, what changed, what is
trending towards trouble. Everything comes from data the platform already
holds plus one fresh verification snapshot — the report never re-polls the
whole network on its own schedule.

Markdown is the source format; HTML is rendered from it with a minimal
converter so the report can be mailed, attached to a ticket, or committed to
a repository without a rendering service.
"""

from __future__ import annotations

import html
import time
from datetime import datetime

from fathom.data.store import Store
from fathom.observe import verify


def _ts(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "—"


def _uptime(sec: int | None) -> str:
    if sec is None:
        return "—"
    d, rem = divmod(int(sec), 86400)
    return f"{d}d {rem // 3600}h"


def build(store: Store, transport, now: float | None = None, window: float = 86400) -> str:
    now = now or time.time()
    since = now - window
    checks = verify.checks(verify.snapshot(store, transport))
    incidents = store.rows("SELECT * FROM incidents WHERE status='open' ORDER BY id")
    recent = store.rows("SELECT * FROM incidents WHERE opened >= ? ORDER BY id", (since,))
    devices = store.rows("SELECT * FROM devices ORDER BY role, name")
    busy = store.rows("SELECT device, name, util FROM interfaces WHERE util IS NOT NULL ORDER BY util DESC LIMIT 5")
    noisy = store.rows("SELECT device, name, err_per_min FROM interfaces WHERE err_per_min > 0 ORDER BY err_per_min DESC LIMIT 5")
    changes = store.rows("SELECT ts, actor, action, device FROM audit WHERE action IN ('config.apply','job.succeeded') "
                         "AND ts >= ? ORDER BY ts", (since,))
    backups = store.rows("SELECT device, COUNT(*) n FROM config_backups WHERE ts >= ? GROUP BY device", (since,))

    healthy = all(c.passed for c in checks) and not incidents
    verdict = "✅ Healthy — no open incidents and all checks passed." if healthy else \
        f"⚠️ Attention needed — {len(incidents)} open incident(s), " \
        f"{sum(not c.passed for c in checks)} failing check(s)."

    md = ["# Network inspection report", f"Generated {_ts(now)} · covering the last {int(window // 3600)} h", "",
          f"**{verdict}**", "", "## Checks", "", "| Check | Result | Detail |", "|---|---|---|"]
    md += [f"| {c.name} | {'pass' if c.passed else '**FAIL**'} | {c.detail} |" for c in checks]

    md += ["", "## Open incidents", ""]
    md += [f"- #{i['id']} {i['title']} (since {_ts(i['opened'])}, confidence {i['confidence']})" for i in incidents] \
        or ["None."]
    md += ["", f"Incidents opened in the period: {len(recent)}."]

    md += ["", "## Devices", "", "| Device | Role | Vendor | Reachable | CPU | Uptime |", "|---|---|---|---|---|---|"]
    for d in devices:
        cpu = "—" if d["cpu"] is None else f"{d['cpu']:.0f}%"
        md.append(f"| {d['name']} | {d['role']} | {d['vendor']} | {'yes' if d['reachable'] else '**no**'} | "
                  f"{cpu} | {_uptime(d['uptime'])} |")

    md += ["", "## Capacity and errors", "", "Busiest interfaces:", ""]
    md += [f"- {b['device']} {b['name']}: {b['util']:.1f}%" for b in busy] or ["- no data yet"]
    md += ["", "Interfaces with errors:", ""]
    md += [f"- {n['device']} {n['name']}: {n['err_per_min']:.0f}/min" for n in noisy] or ["- none"]

    md += ["", "## Changes", ""]
    md += [f"- {_ts(c['ts'])} {c['actor']} {c['action']} {c['device']}" for c in changes] or ["- none"]
    md += ["- configuration versions saved: " + (", ".join(f"{b['device']} ({b['n']})" for b in backups) or "none")]
    return "\n".join(md) + "\n"


def to_html(md: str) -> str:
    """Just enough Markdown for this report: headings, bold, lists, tables."""
    out, in_list, in_table = [], False, False

    def inline(t: str) -> str:
        t = html.escape(t)
        parts = t.split("**")
        return "".join(f"<strong>{p}</strong>" if i % 2 else p for i, p in enumerate(parts))

    for line in md.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set("".join(cells)) <= {"-"}:
                continue
            if not in_table:
                out.append("<table>")
                tag, in_table = "th", True
            else:
                tag = "td"
            out.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    if in_table:
        out.append("</table>")
    style = ("body{font:14px/1.5 -apple-system,Segoe UI,sans-serif;max-width:860px;margin:32px auto;padding:0 16px;color:#1b1f24}"
             "table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #e3e6ea;padding:6px 8px;text-align:left}"
             "th{font-size:12px;text-transform:uppercase;color:#5b6570}h2{margin-top:28px}")
    return f"<!doctype html><meta charset=utf-8><title>Inspection report</title><style>{style}</style>" + "\n".join(out)
