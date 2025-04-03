# Infrastructure link down

## Symptoms
- `link_down` alert on one or both ends of an uplink or inter-switch link.
- If the far device is single-homed, it also becomes unreachable.
- Syslog: `%LINK-3-UPDOWN` (Cisco) or `IFNET/2/LINK_STATE` (Huawei).

## Diagnosis
1. Check whether both ends report down. If only one end is visible, the far device may have lost power.
2. Look at the error counters on the link before it failed. Rising CRC errors before the drop point to the cable or optic.
3. Check for recent configuration changes on either end in the audit log; an accidental `shutdown` looks identical in counters.
4. If the link is redundant, confirm traffic has moved to the other path and utilisation there is safe.

## Resolution
- Reseat or replace the patch cable or optic; check the light levels on fibre.
- If an end was administratively shut by mistake, re-enable it through a reviewed change.
- After repair, watch the error counters for 15 minutes before closing the incident.
