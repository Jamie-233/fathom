# Port err-disabled by BPDU guard

## Symptoms
- `port_err_disabled` alert; the port is down although nobody shut it.
- Syslog: `%PM-4-ERR_DISABLE: bpduguard error` (Cisco) or `BPDU-Protection ... will be shutdown` (Huawei).

## Cause
A device that speaks spanning tree — usually a small unmanaged or consumer switch — was plugged into a user port. BPDU guard shuts the port to prevent a loop.

## Resolution
- Fathom re-enables the port automatically, at most twice per hour.
- If it trips again, the switch is still connected. Find and remove it before re-enabling; repeated recovery only creates repeated short loops.
- Escalated jobs wait for approval for this reason.
