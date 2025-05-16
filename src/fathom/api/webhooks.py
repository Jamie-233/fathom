"""Inbound NetBox webhooks: the source of truth drives the network.

When someone edits an interface in NetBox — a new description, enabled or
disabled — NetBox calls this endpoint and the change is pushed to the device
through the normal plan/apply path.

Three properties make that safe to leave running:

* **Authenticity.** NetBox signs each body with HMAC-SHA512 over a shared
  secret (``X-Hook-Signature``). Without checking it, anyone who can reach
  the endpoint can reconfigure switches. The comparison is constant-time.
* **Idempotency.** NetBox retries on timeouts, so the same event can arrive
  twice. Requests are deduplicated by NetBox's ``request_id``, and applying
  an already-applied state produces an empty plan anyway.
* **No loops.** If a device change were synced *back* into NetBox, NetBox
  would fire a webhook, which would trigger a change, and so on. Because the
  handler only acts when the plan is non-empty, an echo of a state the device
  already has stops on the first round.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections import OrderedDict

from fastapi import APIRouter, Header, HTTPException, Request

from fathom.delivery import iac
from fathom.observe import verify
from fathom.platform import Platform


def netbox_router(p: Platform, secret: str | None = None) -> APIRouter:
    router = APIRouter()
    secret = secret if secret is not None else os.environ.get("FATHOM_NETBOX_SECRET", "")
    seen: OrderedDict[str, dict] = OrderedDict()

    @router.post("/api/webhooks/netbox")
    async def netbox(request: Request, x_hook_signature: str = Header(default="")):
        body = await request.body()
        if secret:
            expected = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
            if not hmac.compare_digest(expected, x_hook_signature):
                raise HTTPException(403, "bad signature")
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(400, "body is not JSON")

        rid = str(event.get("request_id") or "")
        if rid and rid in seen:
            return {**seen[rid], "duplicate": True}

        if event.get("model") != "interface" or event.get("event") not in ("created", "updated"):
            return {"status": "ignored", "reason": f"{event.get('model')}/{event.get('event')} is not handled"}

        data = event.get("data") or {}
        device = (data.get("device") or {}).get("name")
        name = data.get("name")
        if not device or not name:
            raise HTTPException(422, "interface event without device or name")
        want = {k: data[k] for k in ("description", "enabled") if k in data}

        try:
            with p.lock:
                plan = iac.plan(p.store, p.transport, {device: {"interfaces": {name: want}}})
                if plan.change_count == 0:
                    result = {"status": "noop", "reason": "device already matches NetBox"}
                else:
                    if want.get("enabled") is False and p.store.row(
                            "SELECT 1 FROM links WHERE a=? AND a_if=?", (device, name)):
                        raise HTTPException(409, f"{device} {name} is an infrastructure link; not disabling from NetBox")
                    before = verify.snapshot(p.store, p.transport)
                    applied = iac.apply(p.store, p.transport, plan, actor="netbox")
                    failed = [c.name for c in verify.compare(before, verify.snapshot(p.store, p.transport))
                              if not c.passed]
                    result = {"status": "applied" if all(a["ok"] for a in applied) else "failed",
                              "changes": plan.change_count, "results": applied, "regressions": failed}
        except iac.PlanError as exc:
            raise HTTPException(422, str(exc))

        if rid:
            seen[rid] = result
            while len(seen) > 1000:
                seen.popitem(last=False)
        return result

    return router
