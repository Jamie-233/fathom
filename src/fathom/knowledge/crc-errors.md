# CRC and input errors on a port

## Symptoms
- `interface_errors` alert: sustained input errors per minute.
- Users on the port report slow or dropped connections; the link stays up.

## Diagnosis
1. CRC errors on a copper user port are almost always physical: damaged cable, bad keystone, or a failing NIC.
2. Duplex mismatch also produces errors; check that both sides auto-negotiate.
3. On fibre uplinks check optic temperature and receive power; a dirty connector raises errors gradually.

## Resolution
- On a user port, Fathom can isolate the port (shut it and label it) after approval, so a bad cable stops affecting the switch.
- Replace the patch cable first, then the wall cabling, then test the end device NIC.
- Never shut an uplink to stop errors: degraded is better than down. Schedule an optic or cable replacement instead.
