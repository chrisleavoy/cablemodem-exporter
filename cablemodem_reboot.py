#!/usr/bin/env python3
"""Reboot the cable modem, as its web UI's Restart button does. Never a factory reset.

Reads the same settings as the exporter (CM_HOST, CM_USERNAME, CM_PASSWORD or
CM_PASSWORD_FILE), from the environment or --env-file.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import cablemodem_client as client
from cablemodem_client import ModemError

DEFAULT_ENV_FILE = Path("/etc/cablemodem-exporter/env")
PROBE_INTERVAL = 15

# Indirection so tests can drive --wait with a virtual clock.
clock = time.monotonic
sleep = time.sleep


def positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, got {text!r}")
    return value


def read_system(modem: client.Modem) -> tuple[ET.Element, tuple[float, float] | None]:
    """The modem's system info, and the earliest and latest moment (on clock()) it can have booted.

    The uptime was read somewhere between sending the request and getting the
    answer, and is rounded down to whole seconds, so the boot happened after
    (sent - uptime - 1) and no later than (answered - uptime).
    """
    sent = clock()
    system = modem.get(client.FUN_SYSTEM)
    answered = clock()
    uptime = client.parse_uptime(system.findtext("cm_system_uptime", ""))
    return system, (sent - uptime - 1, answered - uptime) if uptime is not None else None


def wait_for_restart(modem: client.Modem, old_boot_latest: float, timeout: float) -> bool:
    """Wait until the modem is OPERATIONAL again after a boot later than old_boot_latest.

    Being reachable, or having a small uptime, isn't enough: the earliest the
    modem can have booted must be after the latest it can have booted before the
    reboot was requested. If the readings can't tell the two boots apart, that
    isn't counted as a restart. Returns False if no restart is confirmed before
    the deadline.
    """
    deadline = clock() + timeout
    request_timeout = modem.timeout
    while (remaining := deadline - clock()) > 0:
        sleep(min(PROBE_INTERVAL, remaining))
        remaining = deadline - clock()
        if remaining <= 0:
            break
        modem.timeout = min(request_timeout, remaining)
        try:
            system, booted = read_system(modem)
        except ModemError as e:
            print(f"  not reachable yet ({e})")
            continue
        finally:
            modem.timeout = request_timeout
        status = system.findtext("cm_status", "")
        restarted = booted is not None and booted[0] > old_boot_latest
        print(f"  status {status}, {'restarted' if restarted else 'no restart seen yet'}")
        if status == "OPERATIONAL" and restarted and clock() <= deadline:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yes", action="store_true", help="reboot without asking for confirmation")
    parser.add_argument("--wait", action="store_true",
                        help="wait until the modem has restarted and is OPERATIONAL again")
    parser.add_argument("--wait-timeout", type=positive_seconds, default=600, metavar="SECONDS",
                        help="give up waiting after this long (default: %(default)s)")
    parser.add_argument("--env-file", type=Path,
                        help=f"settings file to read (default: {DEFAULT_ENV_FILE} if it exists)")
    args = parser.parse_args(argv)

    env_file = args.env_file or (DEFAULT_ENV_FILE if DEFAULT_ENV_FILE.exists() else None)
    if env_file:
        client.load_env_file(env_file)
    modem = client.modem_from_env()
    try:
        return reboot(modem, args)
    finally:
        modem.close()


def reboot(modem: client.Modem, args: argparse.Namespace) -> int:
    try:
        system, old_boot = read_system(modem)
    except ModemError as e:
        print(f"Can't read the modem's status: {e}", file=sys.stderr)
        return 1
    print(f"Modem {modem.base}: {system.findtext('HwModel')} {system.findtext('SwVersion')}, "
          f"status {system.findtext('cm_status')}, up {system.findtext('cm_system_uptime')}")

    if not args.yes:
        answer = input("Reboot it? Internet access drops for several minutes. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Not rebooting.")
            return 1

    # Without an uptime reading, only a boot after the request itself counts.
    old_boot_latest = old_boot[1] if old_boot else clock()
    try:
        modem.reboot()
    except ModemError as e:
        print(f"Reboot failed: {e}", file=sys.stderr)
        return 1
    print("Reboot requested; the modem usually takes 3-5 minutes to come back.")

    if args.wait:
        if not wait_for_restart(modem, old_boot_latest, args.wait_timeout):
            print(f"Couldn't confirm the modem restarted and is OPERATIONAL within {args.wait_timeout:g}s",
                  file=sys.stderr)
            return 2
        print("Modem has restarted and is OPERATIONAL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
