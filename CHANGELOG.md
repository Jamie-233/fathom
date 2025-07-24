# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-11-12

### Added

- The SSH transport is wired to the platform: `--transport ssh` reads the
  device list from the topology file, and `FATHOM_SSH_USER` /
  `FATHOM_SSH_PASSWORD` supply credentials. Missing credentials and a refused
  connection now fail with a message instead of a traceback.
- Tests replace Netmiko with a fake, covering command dispatch, vendor
  mapping, session reuse, reconnect after a dropped session, timeouts, the
  syslog queue and the transport factory.

## [0.2.0] - 2026-11-12

### Fixed

- The topology adjacency was built one direction at a time and relied on LLDP
  reporting from both ends. A device that was dark during discovery, or a link
  that only reported one way, left the graph directed, so the path search and
  `device_dead()` silently lost those links. Both directions are now recorded
  and deduplicated, which leaves the symmetric case unchanged.
- Write endpoints accepted requests without authentication whenever
  `FATHOM_TOKEN` was unset. They now answer `503` until a token is configured.
  The local demo opts in explicitly (`--host 127.0.0.1` or
  `FATHOM_ALLOW_ANONYMOUS_WRITES=1`); binding wider without a token prints a
  warning.

### Changed

- README: the package map replaces the course-oriented section.

## [0.1.0] - 2026-09-01

- Initial release: collection and parsing, SQLite inventory and telemetry,
  alert rules, topology-aware RCA, safe remediation, config as code, REST API,
  portal, lab simulator and MCP server.
