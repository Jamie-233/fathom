# High control-plane CPU

## Symptoms
- `high_cpu` alert: CPU above 80% for several polls.
- Slow SSH sessions, routing protocol flaps, delayed SNMP or polling responses.

## Diagnosis
1. Fathom collects CPU and version output automatically when the alert fires; read the job output first.
2. Common causes: a broadcast or multicast storm punted to the CPU, a routing protocol churn, excessive logging or debugging left enabled.
3. Correlate with interface utilisation: a storm shows as a sudden traffic jump on access ports.

## Resolution
- Remove any `debug` commands and reduce logging levels.
- Enable storm control on access ports.
- If a single process dominates after a software upgrade, check the vendor bug database for the running version.
