"""Client for the web UI of Compal/CBN cable modems (tested on the CD8000).

The UI is a jQuery app that POSTs a numeric "fun" code to /xml/getter.xml (reads)
or /xml/setter.xml (actions), always with the current sessionToken cookie as the
first form field. The modem rotates that token on every response and drops
requests that don't look like they came from its own UI.

fun codes on CD8000-7.13.198.12-NOSH, from the UI's JavaScript and a read-only
scan of getter.xml 1..20:

  getter.xml (reads)
     1  GlobalSettings: access level, firmware, operator ids      (unused)
     2  cm_system_info: model, firmware, serial, uptime, status   (exporter)
     3  multilang: UI languages                                   (unused)
     5  status: cm_status only, a subset of 2                     (unused)
     6  upstreamOFDMA_table                                       (exporter)
     9  downstreamOFDM_table                                      (exporter)
    10  downstream_table (SC-QAM)                                 (exporter)
    11  upstream_table (ATDMA)                                    (exporter)
    13  eventlog_table                                            (exporter)
    4, 7, 8, 14, 16, 17 answer "invalid operation": they are setter functions.
    12, 15, 18+ answer with the HTML index page: not functions here.

  setter.xml (actions), as used by the UI
     4  set UI language
     7  RESET TO FACTORY SETTINGS  -- never sent by this code
     8  Restart (reboot)           -- reboot() only, after verifying the UI
    15  login (Username, Password) -> "successful;SID=<n>"
    16  logout
    14 and 17 are never called by the UI; their purpose is unknown, so don't.

This module deliberately has no generic "call setter" method: the only actions
it can perform are login and reboot.
"""

from __future__ import annotations

import math
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

FUN_SYSTEM = 2
FUN_US_OFDMA = 6
FUN_DS_OFDM = 9
FUN_DS = 10
FUN_US = 11
FUN_EVENTLOG = 13
FUN_LOGIN = 15
FUN_REBOOT = 8

# Root element each getter returns; anything else is a protocol error.
ROOT_TAGS = {
    FUN_SYSTEM: "cm_system_info",
    FUN_US_OFDMA: "upstreamOFDMA_table",
    FUN_DS_OFDM: "downstreamOFDM_table",
    FUN_DS: "downstream_table",
    FUN_US: "upstream_table",
    FUN_EVENTLOG: "eventlog_table",
}

LOGIN_RE = re.compile(r"successful;SID=(\d+)")


class ModemError(Exception):
    """A failure talking to the modem. kind is "auth", "protocol" or "transport"."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name) or str(default)
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise SystemExit(f"{name} must be a positive number, got {raw!r}")
    return value


def load_env_file(path: Path) -> None:
    """Read plain KEY=value lines without overriding variables already set.

    This is the subset of systemd's EnvironmentFile syntax that both systemd and
    this parser read identically. Quotes and backslashes mean something to
    systemd, so they're rejected rather than read differently; a password that
    needs them belongs in a file named by CM_PASSWORD_FILE.
    """
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise SystemExit(f"{path}:{number}: expected KEY=value")
        if value[:1] in ("'", '"') or "\\" in value:
            raise SystemExit(f"{path}:{number}: quotes and backslashes aren't supported in {key}; "
                             f"put a password that needs them in a file and set CM_PASSWORD_FILE")
        os.environ.setdefault(key, value)


def modem_from_env() -> Modem:
    password = os.environ.get("CM_PASSWORD", "")
    if os.environ.get("CM_PASSWORD_FILE"):
        password = Path(os.environ["CM_PASSWORD_FILE"]).read_text().strip()
    if not password:
        raise SystemExit("CM_PASSWORD or CM_PASSWORD_FILE is required")
    return Modem(os.environ.get("CM_HOST", "192.168.100.1"), os.environ.get("CM_USERNAME", "cusadmin"),
                 password, timeout=env_float("CM_TIMEOUT", 10))


def parse_uptime(text: str) -> int | None:
    """Seconds from the modem's "1day(s)2h:3m:4s" format."""
    match = re.fullmatch(r"(\d+)day\(s\)(\d+)h:(\d+)m:(\d+)s", text.strip())
    if not match:
        return None
    days, hours, minutes, seconds = map(int, match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def button_fun(page: str, button_id: str) -> int | None:
    """The fun code a UI button's click handler sends, e.g. ajaxSet({'fun':8})."""
    match = re.search(r"\$\('#" + re.escape(button_id) + r"'\)\.click\(\s*function\s*\(\s*\)\s*\{"
                      r"[^{}]*?ajaxSet\(\{\s*'fun'\s*:\s*(\d+)\s*\}\)", page)
    return int(match.group(1)) if match else None


class Modem:
    def __init__(self, host: str, username: str, password: str, timeout: float = 10.0):
        self.base = f"http://{host}"
        self.username = username
        self.password = password
        self.timeout = timeout
        self.logins = 0
        self._session: requests.Session | None = None

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def _request(self, method: str, path: str, data: dict | None = None) -> str:
        try:
            response = self._session.request(method, self.base + path, data=data, timeout=(3, self.timeout))
        except requests.RequestException as e:
            raise ModemError("transport", f"{path}: {type(e).__name__}") from e
        if response.status_code != 200:
            raise ModemError("protocol", f"{path}: HTTP {response.status_code}")
        return response.text

    def _post(self, path: str, fun: int, **fields: str) -> str:
        data = {"token": self._session.cookies.get("sessionToken", ""), "fun": fun, **fields}
        return self._request("POST", f"/xml/{path}", data)

    def login(self) -> None:
        self.close()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest",
                                      "Referer": self.base + "/index.html", "Origin": self.base})
        try:
            self._request("GET", "/common_page/login.html")  # issues the first sessionToken
            body = self._post("setter.xml", FUN_LOGIN, Username=self.username, Password=self.password)
        except ModemError:
            self.close()
            raise
        match = LOGIN_RE.fullmatch(body.strip())
        if not match:
            self.close()
            raise ModemError("auth", "login rejected")
        self._session.cookies.set("SID", match.group(1))
        self.logins += 1

    def get(self, fun: int) -> ET.Element:
        """Read one getter table, logging in (again) if there's no live session."""
        for attempt in (1, 2):
            if self._session is None:
                self.login()
            try:
                body = self._post("getter.xml", fun).strip()
            except ModemError as e:
                # A reset connection usually means the modem dropped our session.
                if e.kind != "transport" or attempt == 2:
                    raise
                self.close()
                continue
            # An expired session gets an empty body or the HTML index page.
            if not body or body.lower().startswith(("<!doctype", "<html")):
                self.close()
                continue
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                raise ModemError("protocol", f"fun={fun}: response is not XML") from None
            if root.tag != ROOT_TAGS[fun]:
                raise ModemError("protocol", f"fun={fun}: expected <{ROOT_TAGS[fun]}>, got <{root.tag}>")
            return root
        raise ModemError("auth", f"fun={fun}: session rejected right after logging in")

    def reboot(self) -> None:
        """Press the web UI's Restart button.

        On the CD8000, setter fun=8 is "Restart" and fun=7 is "RESET TO FACTORY
        SETTINGS". Before sending anything, check this modem's own settings page
        wires the Restart button to fun=8 (and the factory reset button to
        something else), so firmware that renumbered its functions can't turn
        this into a factory reset.
        """
        self.login()  # fresh session, so the page below can't be a logged-out redirect
        page = self._request("GET", "/common_page/RgConfig.html")
        restart = button_fun(page, "id_common_restart")
        factory_reset = button_fun(page, "id_common_restore_factory_defaults")
        if restart != FUN_REBOOT or factory_reset is None or factory_reset == FUN_REBOOT:
            raise ModemError("protocol", f"refusing to reboot: the UI's Restart button sends fun={restart} and "
                                         f"factory reset sends fun={factory_reset}, expected 8 and something else")
        body = self._post("setter.xml", FUN_REBOOT).strip()
        if body:  # the UI treats any response text as an error (e.g. "GUILogin" when logged out)
            raise ModemError("protocol", f"modem refused the reboot: {body[:60]!r}")
