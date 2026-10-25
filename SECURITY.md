# Security

Please report vulnerabilities privately using GitHub's **Report a vulnerability** button on this
repository's Security tab, not in a public issue. Include the exporter version
(`cablemodem_exporter_build_info`), modem model/firmware, and steps to reproduce.

Deployment notes:

- The modem's web UI only speaks plaintext HTTP, so the password crosses the LAN segment between
  the exporter host and the modem unencrypted. Run the exporter on a host directly on that segment.
- The metrics endpoint binds to `127.0.0.1` by default. If you expose it more widely, put it
  behind a firewall or reverse proxy with authentication: it reveals the modem's model, firmware
  and serial number and, when enabled (`CM_EXPORT_EVENT_MESSAGES`), raw event log text.
- Keep the password in the root-owned `EnvironmentFile` (mode 0600) or point `CM_PASSWORD_FILE`
  at a credential file; never commit it.
- `cablemodem-reboot` can take your internet connection down for several minutes; only give
  people access to it (and to the password) if they may do that.
- When contributing fixtures, replace serial numbers with `GD0000000000` and MAC addresses with
  `00:00:00:00:00:0N`. A test fails if anything else that looks like either is committed.
