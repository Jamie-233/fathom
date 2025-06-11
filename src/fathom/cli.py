"""Command-line entry points.

``serve`` runs the platform and portal. The other commands run one task
against a fresh in-process lab, which is what CI pipelines and course
exercises want: deterministic, no server to start, exit code tells the
result. ``mcp`` connects to a running server instead, because an IDE should
see the same live network the portal shows.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from fathom import __version__


def _platform(args):
    from fathom.platform import Platform

    p = Platform(db=getattr(args, "db", ":memory:"), seed=getattr(args, "seed", 7))
    p.cycle()
    return p


def cmd_serve(args) -> int:
    import uvicorn

    from fathom.api.app import create_app
    from fathom.platform import Platform

    hooks = [{"url": u, "format": "alertmanager"} for u in args.alertmanager_webhook or []] + \
            (args.webhook or [])
    p = Platform(db=args.db, webhooks=hooks, seed=args.seed, sim_seconds=args.sim_seconds)
    p.start(interval=args.interval)
    try:
        uvicorn.run(create_app(p), host=args.host, port=args.port, log_level="warning")
    finally:
        p.stop()
    return 0


def cmd_plan(args) -> int:
    from fathom.delivery import iac

    p = _platform(args)
    plan = iac.plan(p.store, p.transport, iac.load_desired(args.file))
    for d in plan.devices:
        if d.error:
            print(f"! {d.device}: {d.error}")
        for c in d.changes:
            print(f"~ {d.device} {c.interface} {c.field}: {c.before!r} -> {c.after!r}")
    print(f"\nPlan: {plan.change_count} change(s)")
    if args.json:
        print(json.dumps(plan.to_dict(), indent=2))
    # Exit 2 on drift, like `terraform plan -detailed-exitcode`, so CI can gate.
    return 2 if plan.change_count else 0


def cmd_verify(args) -> int:
    from fathom.observe import verify

    p = _platform(args)
    results = verify.checks(verify.snapshot(p.store, p.transport))
    for c in results:
        print(f"{'PASS' if c.passed else 'FAIL'}  {c.name:<24} {c.detail}")
    return 0 if all(c.passed for c in results) else 1


def cmd_report(args) -> int:
    from fathom.ops import report

    p = _platform(args)
    md = report.build(p.store, p.transport, now=p.lab.clock)
    sys.stdout.write(report.to_html(md) if args.html else md)
    return 0


def cmd_ask(args) -> int:
    from fathom.ai.agent import Agent

    p = _platform(args)
    for fault in args.fault or []:
        kind, dev, port = (fault.split(":") + ["", ""])[:3]
        {"power_off": lambda: p.lab.power(dev, False),
         "link_down": lambda: p.lab.fail_link(dev, port),
         "crc_errors": lambda: p.lab.corrupt_port(dev, port),
         "cpu": lambda: p.lab.cpu_spike(dev)}[kind]()
    for _ in range(3):
        p.cycle()
    ans = Agent(p).ask(args.question)
    print(ans.text)
    return 0


def cmd_mcp(args) -> int:
    from fathom.ai.mcp import main as mcp_main

    mcp_main()
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="fathom", description="AIOps platform for networks")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the platform, API and portal")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--db", default="fathom.db")
    s.add_argument("--interval", type=float, default=2.0, help="real seconds between cycles")
    s.add_argument("--sim-seconds", type=float, default=30.0, help="simulated seconds per cycle")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--webhook", action="append", help="POST incident events here (simple JSON)")
    s.add_argument("--alertmanager-webhook", action="append", help="POST in Alertmanager webhook format")
    s.set_defaults(fn=cmd_serve)

    pl = sub.add_parser("plan", help="show changes needed to reach a desired state")
    pl.add_argument("file")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(fn=cmd_plan)

    v = sub.add_parser("verify", help="run live network checks")
    v.set_defaults(fn=cmd_verify)

    r = sub.add_parser("report", help="print an inspection report")
    r.add_argument("--html", action="store_true")
    r.set_defaults(fn=cmd_report)

    a = sub.add_parser("ask", help="ask the operations agent (offline mode without an LLM)")
    a.add_argument("question")
    a.add_argument("--fault", action="append", help="inject first: power_off:DEV, link_down:DEV:PORT, crc_errors:DEV:PORT, cpu:DEV")
    a.set_defaults(fn=cmd_ask)

    m = sub.add_parser("mcp", help="MCP stdio server for a running Fathom (FATHOM_URL, FATHOM_TOKEN)")
    m.set_defaults(fn=cmd_mcp)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
