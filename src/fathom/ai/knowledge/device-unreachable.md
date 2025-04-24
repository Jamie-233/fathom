# Device unreachable

## Symptoms
- `device_unreachable` alert: the poller gets no response.
- Neighbours may report the links to it as down.

## Diagnosis
1. Read the incident's root cause first. If Fathom attributes it to an upstream device or link, the device itself is probably healthy and only cut off.
2. If all neighbours see their links to the device as down, it has lost power or crashed.
3. A single-homed device behind one failed link looks exactly like a dead device from the network. Check from the closest reachable neighbour, or physically.

## Resolution
- Restore the upstream path first; isolated devices recover by themselves.
- For a dead device: check power and the UPS, then the console for a crash or boot loop.
- After recovery, compare uptime: a reset uptime confirms the device rebooted rather than only being cut off.
