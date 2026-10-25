"""Tests. Run: python3 -m pytest (or python3 -m unittest discover -s tests)"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cablemodem_client as client  # noqa: E402
import cablemodem_exporter as cm  # noqa: E402
import cablemodem_reboot  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_FILES = {client.FUN_SYSTEM: "fun2.xml", client.FUN_US_OFDMA: "fun6.xml", client.FUN_DS_OFDM: "fun9.xml",
                 client.FUN_DS: "fun10.xml", client.FUN_US: "fun11.xml", client.FUN_EVENTLOG: "fun13.xml"}
PASSWORD = "test-password"
FACTORY_RESET = 7


def fixture(fun: int) -> str:
    return (FIXTURES / FIXTURE_FILES[fun]).read_text()


def scrape(exporter: cm.Exporter) -> dict:
    """{(name, labels): value} as Prometheus would see it."""
    registry = CollectorRegistry()
    registry.register(exporter)
    samples = {}
    for family in text_string_to_metric_families(generate_latest(registry).decode()):
        for s in family.samples:
            samples[(s.name, frozenset(s.labels.items()))] = s.value
    return samples


def value(samples: dict, name: str, **labels) -> float | None:
    return samples.get((name, frozenset(labels.items())))


def names(samples: dict) -> set[str]:
    return {name for name, _ in samples}


class FakeModem:
    """Mimics the CD8000 web UI: rotating sessionToken, fun=15 login, SID cookie, setter actions."""

    def __init__(self):
        self.responses = {fun: fixture(fun) for fun in FIXTURE_FILES}
        self.settings_page = (FIXTURES / "RgConfig.html").read_text()
        self.setter_calls: list[int] = []
        self.logins = 0
        self.valid_sid: str | None = None
        self.token = 1000
        self.status = 200
        self.login_body: str | None = None
        self.reboot_body = ""
        modem = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, body: str, status: int = 200):
                modem.token += 1
                data = body.encode()
                self.send_response(status)
                self.send_header("Set-Cookie", f"sessionToken={modem.token}; path=/;")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _logged_in(self) -> bool:
                cookies = dict(c.strip().split("=", 1) for c in self.headers.get("Cookie", "").split(";") if "=" in c)
                self.cookies = cookies
                return modem.valid_sid is not None and cookies.get("SID") == modem.valid_sid

            def do_GET(self):
                if self.path == "/common_page/RgConfig.html" and self._logged_in():
                    return self._reply(modem.settings_page)
                self._reply("<html>login</html>")

            def do_POST(self):
                form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
                logged_in = self._logged_in()
                if modem.status != 200:
                    return self._reply("", modem.status)
                if list(form)[:1] != ["token"] or form["token"] != self.cookies.get("sessionToken"):
                    return self._reply("")
                fun = int(form["fun"])
                if self.path == "/xml/setter.xml":
                    modem.setter_calls.append(fun)
                    if fun == client.FUN_LOGIN:
                        if modem.login_body is not None:
                            return self._reply(modem.login_body)
                        if form.get("Username") == "cusadmin" and form.get("Password") == PASSWORD:
                            modem.logins += 1
                            modem.valid_sid = str(4000 + modem.logins)
                            return self._reply(f"successful;SID={modem.valid_sid}")
                        return self._reply("failed")
                    return self._reply(modem.reboot_body if logged_in else "GUILogin")
                if not logged_in:
                    return self._reply("<!doctype html><html>index</html>")
                self._reply(modem.responses[fun])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeModemTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeModem()
        self.modem = client.Modem(self.fake.host, "cusadmin", PASSWORD, timeout=2)
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.exporter = self.new_exporter()

    def tearDown(self):
        self.modem.close()
        self.fake.close()
        self._tmp.cleanup()

    def new_exporter(self, **kwargs) -> cm.Exporter:
        """A fresh exporter process, sharing the state directory with earlier ones."""
        return cm.Exporter(self.modem, cm.EventLogTracker(self.state / "snapshot.json"), **kwargs)

    def poll(self) -> dict:
        self.exporter.poll()
        return scrape(self.exporter)


# --------------------------------------------------------------------- client

class ClientTests(FakeModemTestCase):
    def test_reads_each_table(self):
        for fun, tag in client.ROOT_TAGS.items():
            self.assertEqual(self.modem.get(fun).tag, tag)
        self.assertEqual(self.fake.logins, 1)

    def test_logs_in_again_when_session_expires(self):
        self.modem.get(client.FUN_SYSTEM)
        self.fake.valid_sid = None
        self.modem.get(client.FUN_SYSTEM)
        self.assertEqual(self.fake.logins, 2)

    def assert_error(self, kind: str, modem: client.Modem | None = None):
        with self.assertRaises(client.ModemError) as caught:
            (modem or self.modem).get(client.FUN_SYSTEM)
        self.assertEqual(caught.exception.kind, kind)

    def test_wrong_password(self):
        self.modem.password = "wrong"
        self.assert_error("auth")

    def test_login_reply_without_sid(self):
        self.fake.login_body = "successful"
        self.assert_error("auth")

    def test_unexpected_root_element(self):
        self.fake.responses[client.FUN_SYSTEM] = "<root/>"
        self.assert_error("protocol")

    def test_malformed_xml(self):
        self.fake.responses[client.FUN_SYSTEM] = "<cm_system_info>"
        self.assert_error("protocol")

    def test_http_error(self):
        self.fake.status = 500
        self.assert_error("protocol")

    def test_unreachable_modem(self):
        self.assert_error("transport", client.Modem("127.0.0.1:1", "cusadmin", PASSWORD, timeout=1))

    def test_xml_without_declaration_is_accepted(self):
        self.fake.responses[client.FUN_US] = fixture(client.FUN_US).split("?>", 1)[1]
        self.assertEqual(self.modem.get(client.FUN_US).tag, "upstream_table")


class EnvFileTests(unittest.TestCase):
    def load(self, text: str, environ: dict | None = None) -> dict:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, environ or {}, clear=True):
            path = Path(tmp) / "env"
            path.write_text(text)
            client.load_env_file(path)
            return dict(os.environ)

    def test_plain_values_comments_and_blanks(self):
        env = self.load("# comment\n; also a comment\n\nCM_HOST=10.0.0.1\nCM_PASSWORD = p@ss w0rd\nCM_EMPTY=\n")
        self.assertEqual((env["CM_HOST"], env["CM_PASSWORD"], env["CM_EMPTY"]), ("10.0.0.1", "p@ss w0rd", ""))

    def test_environment_takes_precedence(self):
        self.assertEqual(self.load("CM_HOST=file\n", {"CM_HOST": "env"})["CM_HOST"], "env")

    def test_syntax_systemd_reads_differently_is_rejected(self):
        for line in ('CM_PASSWORD="test password"', "CM_PASSWORD='x'", r"CM_PASSWORD=a\b", "not a setting",
                     "1BAD=x"):
            with self.subTest(line=line), self.assertRaises(SystemExit):
                self.load(line + "\n")

    def test_password_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pw"
            path.write_text('"quoted" \\ password\n')
            with mock.patch.dict(os.environ, {"CM_PASSWORD_FILE": str(path)}, clear=True):
                self.assertEqual(client.modem_from_env().password, '"quoted" \\ password')

    def test_numbers_must_be_finite_and_positive(self):
        for bad in ("nan", "inf", "-1", "0", "x"):
            with self.subTest(bad=bad), mock.patch.dict(os.environ, {"CM_TIMEOUT": bad}):
                with self.assertRaises(SystemExit):
                    client.env_float("CM_TIMEOUT", 10)


# --------------------------------------------------------------------- reboot

class RebootTests(FakeModemTestCase):
    def test_real_settings_page_maps_restart_to_8_and_factory_reset_to_7(self):
        page = self.fake.settings_page
        self.assertEqual(client.button_fun(page, "id_common_restart"), 8)
        self.assertEqual(client.button_fun(page, "id_common_restore_factory_defaults"), FACTORY_RESET)

    def test_reboot_sends_only_login_and_restart(self):
        self.modem.reboot()
        self.assertEqual(self.fake.setter_calls, [client.FUN_LOGIN, client.FUN_REBOOT])

    def test_refuses_if_restart_button_is_not_fun_8(self):
        swapped = (self.fake.settings_page.replace("'fun':8", "'fun':X").replace("'fun':7", "'fun':8")
                   .replace("'fun':X", "'fun':7"))
        for page in (swapped, "<html>no buttons</html>"):
            with self.subTest(page=page[:20]):
                self.fake.settings_page = page
                self.fake.setter_calls.clear()
                with self.assertRaises(client.ModemError):
                    self.modem.reboot()
                self.assertEqual(self.fake.setter_calls, [client.FUN_LOGIN])

    def test_refused_reboot_is_an_error(self):
        self.fake.reboot_body = "GUILogin"
        with self.assertRaises(client.ModemError):
            self.modem.reboot()

    def cli(self, *args: str, answer: str = "n") -> int:
        env = {"CM_HOST": self.fake.host, "CM_PASSWORD": PASSWORD, "CM_USERNAME": "cusadmin"}
        with mock.patch.dict(os.environ, env), mock.patch("builtins.input", return_value=answer), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return cablemodem_reboot.main([*args, "--env-file", os.devnull])

    def test_cli_asks_before_rebooting(self):
        self.assertEqual(self.cli(answer="n"), 1)
        self.assertNotIn(client.FUN_REBOOT, self.fake.setter_calls)

    def test_cli_reboots_with_yes(self):
        self.assertEqual(self.cli("--yes"), 0)
        self.assertEqual(self.fake.setter_calls[-1], client.FUN_REBOOT)
        self.assertNotIn(FACTORY_RESET, self.fake.setter_calls)

    def test_invalid_wait_timeout_is_rejected_before_any_action(self):
        for bad in ("nan", "inf", "-5", "0", "soon"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.cli("--yes", "--wait", "--wait-timeout", bad)
        self.assertEqual(self.fake.setter_calls, [])

    def test_waiting_reuses_the_session(self):
        self.modem.get(client.FUN_SYSTEM)
        clock = VirtualClock()
        with clock.patch(), mock.patch("sys.stdout"):
            cablemodem_reboot.wait_for_restart(self.modem, clock.now, timeout=60)  # fixture never restarts
        self.assertEqual(self.fake.logins, 1)
        self.fake.valid_sid = None  # session expires once: exactly one more login
        with clock.patch(), mock.patch("sys.stdout"):
            cablemodem_reboot.wait_for_restart(self.modem, clock.now, timeout=60)
        self.assertEqual(self.fake.logins, 2)


class VirtualClock:
    def __init__(self):
        self.now = 1000.0

    def sleep(self, seconds: float):
        self.now += seconds

    def patch(self):
        patcher = mock.patch.multiple(cablemodem_reboot, clock=lambda: self.now, sleep=self.sleep)
        return patcher


class ScriptedModem:
    """Answers status reads from a script of (status, booted_at) pairs or exceptions.

    The uptime it reports is derived from booted_at and the virtual clock, rounded
    down as the real modem's is, at the moment the (virtual) response arrives.
    Each read takes the next latency from `latencies`, cycling. The last script
    step repeats once the script runs out.
    """

    def __init__(self, clock: VirtualClock, script: list, latencies: list[float]):
        self.clock, self.script, self.latencies = clock, list(script), itertools.cycle(latencies)
        self.timeout = 10
        self.probes = 0
        self.timeouts_seen: list[float] = []

    def get(self, fun: int) -> ET.Element:
        self.probes += 1
        self.timeouts_seen.append(self.timeout)
        self.clock.now += next(self.latencies)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        status, booted_at = step
        uptime = int(self.clock.now - booted_at)
        return ET.fromstring(f"<cm_system_info><cm_status>{status}</cm_status>"
                             f"<cm_system_uptime>0day(s)0h:0m:{uptime}s</cm_system_uptime></cm_system_info>")


class WaitForRestartTests(unittest.TestCase):
    """The modem is read once at virtual time 1000, as the CLI does before rebooting, then watched."""

    DOWN = client.ModemError("transport", "down")
    LATENCIES = ([1], [0.2], [4, 0.5, 3], [0.1, 6])

    def wait(self, booted_before: float, script: list, timeout: float = 600,
             latencies: list[float] = (1,)) -> tuple[bool, ScriptedModem]:
        clock = VirtualClock()
        modem = ScriptedModem(clock, [("OPERATIONAL", booted_before), *script], list(latencies))
        with clock.patch(), mock.patch("sys.stdout"):
            _, old_boot = cablemodem_reboot.read_system(modem)
            return cablemodem_reboot.wait_for_restart(modem, old_boot[1], timeout), modem

    def test_genuine_restart(self):
        for latencies in self.LATENCIES:
            with self.subTest(latencies=latencies):
                ok, _ = self.wait(0, [self.DOWN, self.DOWN, ("NOT_READY", 1038), ("OPERATIONAL", 1038)],
                                  latencies=latencies)
                self.assertTrue(ok)

    def test_genuine_restart_shortly_after_a_recent_boot(self):
        # Booted 5s before the request, then really restarted 2s after it.
        for latencies in self.LATENCIES:
            with self.subTest(latencies=latencies):
                ok, _ = self.wait(995, [self.DOWN, ("OPERATIONAL", 1002)], latencies=latencies)
                self.assertTrue(ok)

    def test_recent_boot_that_ignores_the_reboot(self):
        # Small, steadily rising uptimes must not be mistaken for a restart.
        for seconds_before in (1, 5, 15):
            for latencies in self.LATENCIES:
                with self.subTest(seconds_before=seconds_before, latencies=latencies):
                    boot = 1000 - seconds_before
                    ok, _ = self.wait(boot, [("OPERATIONAL", boot)], timeout=120, latencies=latencies)
                    self.assertFalse(ok)

    def test_uptime_that_keeps_increasing(self):
        ok, _ = self.wait(-5000, [("OPERATIONAL", -5000)], timeout=120)
        self.assertFalse(ok)

    def test_unreachable_then_back_without_restarting(self):
        ok, _ = self.wait(0, [self.DOWN, self.DOWN, ("OPERATIONAL", 0)], timeout=120)
        self.assertFalse(ok)

    def test_timeout_shorter_than_probe_interval(self):
        ok, modem = self.wait(0, [("OPERATIONAL", 1005)], timeout=1)
        self.assertFalse(ok)
        self.assertEqual(modem.probes, 1)  # only the reading before the reboot

    def test_answer_arriving_after_the_deadline(self):
        ok, _ = self.wait(0, [("OPERATIONAL", 1010)], timeout=16, latencies=[5])
        self.assertFalse(ok)

    def test_request_timeout_is_capped_by_remaining_budget(self):
        _, modem = self.wait(0, [("OPERATIONAL", 0)], timeout=20)
        self.assertEqual(modem.timeouts_seen[1:], [5])  # first probe, 15s in, has 5s left
        self.assertEqual(modem.timeout, 10)


# ------------------------------------------------------------------- exporter

class PollTests(FakeModemTestCase):
    def test_before_the_first_poll_there_are_no_readings(self):
        s = scrape(self.exporter)
        self.assertEqual(value(s, "cablemodem_up"), 0)
        self.assertEqual(value(s, "cablemodem_events_total", category="reboot"), 0)
        for name in ("cablemodem_operational", "cablemodem_uptime_seconds", "cablemodem_downstream_channels_locked",
                     "cablemodem_last_success_timestamp_seconds", "cablemodem_info"):
            self.assertNotIn(name, names(s))

    def test_poll_publishes_readings(self):
        s = self.poll()
        self.assertEqual(value(s, "cablemodem_up"), 1)
        self.assertEqual(value(s, "cablemodem_downstream_channels_locked"), 30)
        self.assertEqual(value(s, "cablemodem_downstream_snr_db", channel_id="4", frequency_mhz="861"), 37)
        # Unlocked channels report snr=2147483647: no SNR or power for them.
        self.assertIsNone(value(s, "cablemodem_downstream_snr_db", channel_id="34", frequency_mhz="920"))
        self.assertEqual(value(s, "cablemodem_downstream_locked", channel_id="34", frequency_mhz="920"), 0)
        self.assertAlmostEqual(value(s, "cablemodem_upstream_symbol_rate_symbols_per_second",
                                     channel_id="4", frequency_mhz="38.7"), 5.12e6, delta=1)
        self.assertAlmostEqual(value(s, "cablemodem_ofdma_upstream_bandwidth_hertz", channel="0"), 39.2e6, delta=1)
        self.assertEqual(value(s, "cablemodem_uptime_seconds"), 26 * 60 + 27)
        self.assertEqual(value(s, "cablemodem_network_access"), 1)
        info = dict(next(labels for name, labels in s if name == "cablemodem_info"))
        self.assertEqual(info["serial"], "GD0000000000")
        self.assertEqual(value(s, "cablemodem_event_log_entries", priority="warning"), 4)
        self.assertEqual(value(s, "cablemodem_event_log_entries", priority="error"), 0)
        self.assertEqual(value(s, "cablemodem_exporter_logins_total"), 1)

    def test_fields_missing_from_a_successful_poll_are_absent(self):
        self.poll()
        self.fake.responses[client.FUN_SYSTEM] = re.sub(
            r"<cm_system_uptime>.*?</cm_system_uptime>|<cm_network_access>.*?</cm_network_access>", "",
            fixture(client.FUN_SYSTEM))
        s = self.poll()
        self.assertEqual(value(s, "cablemodem_up"), 1)
        self.assertNotIn("cablemodem_uptime_seconds", names(s))
        self.assertNotIn("cablemodem_network_access", names(s))

    def test_failed_poll_leaves_no_modem_readings(self):
        self.poll()
        self.fake.status = 500
        s = self.poll()
        self.assertEqual(value(s, "cablemodem_up"), 0)
        self.assertEqual(value(s, "cablemodem_poll_errors_total", kind="protocol"), 1)
        for name in ("cablemodem_operational", "cablemodem_uptime_seconds", "cablemodem_network_access",
                     "cablemodem_downstream_channels_locked", "cablemodem_downstream_snr_db"):
            self.assertNotIn(name, names(s))
        self.assertIn("cablemodem_last_success_timestamp_seconds", names(s))

    def test_session_is_reused_across_polls(self):
        self.poll()
        self.poll()
        self.assertEqual(self.fake.logins, 1)

    def test_every_sample_is_a_declared_metric(self):
        exporter = self.new_exporter(export_event_messages=True)
        exporter.poll()
        undeclared = {name for name, _, _ in exporter.samples} - set(cm.METRICS)
        self.assertEqual(undeclared, set())


class EventTests(FakeModemTestCase):
    def test_new_events_are_counted_once_and_not_again_after_restart(self):
        self.poll()
        s = self.poll()
        self.assertEqual(value(s, "cablemodem_events_total", category="mdd_recovery"), 3)
        self.assertEqual(value(s, "cablemodem_events_by_priority_total", priority="warning"), 4)
        self.exporter = self.new_exporter()  # restart: counters start over, old entries aren't new
        s = self.poll()
        self.assertEqual(value(s, "cablemodem_events_total", category="mdd_recovery"), 0)

    def test_repeated_identical_lines_are_new(self):
        tracker = cm.EventLogTracker(self.state / "repeat.json")
        line = ("01-01-1970 00:01:06", "notice", "Cable Modem Reboot due to power reset")
        self.assertEqual(tracker.new_entries([line]), [line])
        self.assertEqual(tracker.new_entries([line, line]), [line])
        self.assertEqual(tracker.new_entries([line, line]), [])

    def test_malformed_snapshot_is_ignored(self):
        path = self.state / "bad.json"
        path.write_text(json.dumps([[None, None, 12]]))
        self.assertEqual(cm.EventLogTracker(path).previous, [])

    def test_newest_event_time_never_goes_backwards(self):
        newer = ("23-10-2026 20:00:00", "notice", "REGISTRATION COMPLETE - Waiting for Operational status")
        older = ("22-10-2026 20:00:00", "notice", "REGISTRATION COMPLETE - Waiting for Operational status")
        self.exporter._count_events([newer, older])
        self.assertEqual(self.exporter.newest_event["registration_complete"], cm.parse_event_time(newer[0]))

    def test_event_messages_are_opt_in(self):
        self.assertNotIn("cablemodem_event_log_entry_timestamp_seconds", names(self.poll()))
        self.exporter = self.new_exporter(export_event_messages=True)
        self.assertIn("cablemodem_event_log_entry_timestamp_seconds", names(self.poll()))

    def test_events_go_to_the_journal_at_their_priority(self):
        with self.assertLogs("cablemodem.event", logging.INFO) as logs:
            self.poll()
        self.assertEqual(len(logs.records), 20)
        warning = next(r for r in logs.records if "Lost MDD" in r.getMessage())
        self.assertEqual(warning.syslog_priority, 4)
        self.assertEqual(cm.JournalFormatter("%(message)s").format(warning)[:3], "<4>")

    def test_labels_are_bounded_and_canonical(self):
        status = "CM-STATUS message sent. Event Type Code: {}; Chan ID: {}; DSID: N/A;"
        self.assertEqual(cm.cm_status_labels(status.format(2, "034")), ("2", "QAM/FEC lock failure", "34"))
        self.assertEqual(cm.cm_status_labels(status.format(6, "N/A")), ("6", "T4 timeout", "none"))
        self.assertEqual(cm.cm_status_labels(status.format(99, 1))[1], "code 99")
        self.assertIsNone(cm.cm_status_labels(status.format(123, 1)))
        self.assertIsNone(cm.cm_status_labels(status.format(1, "1234")))
        self.assertEqual(cm.categorize("Warning 123"), cm.OTHER)
        self.assertEqual(cm.categorize("No Ranging Response received - T3 time-out"), "t3_timeout")
        for _, _, message in cm.parse_events(ET.fromstring(fixture(client.FUN_EVENTLOG))):
            self.assertNotEqual(cm.categorize(message), cm.OTHER, message)

    def test_event_time(self):
        self.assertEqual(cm.parse_event_time("23-10-2026 20:03:19"), 1792785799)
        self.assertIsNone(cm.parse_event_time("01-01-1970 00:01:06"))
        self.assertIsNone(cm.parse_event_time("garbage"))


class ParsingTests(unittest.TestCase):
    def test_number_rejects_unusable_values(self):
        el = ET.fromstring("<x><a>N/A</a><b>inf</b><c>nan</c><d> -1.5 </d><e></e></x>")
        self.assertEqual([cm.number(el, t) for t in "abcdef"], [None, None, None, -1.5, None, None])

    def test_identifier_is_canonical(self):
        el = ET.fromstring("<x><a>01</a><b>¹</b><c>x</c></x>")
        self.assertEqual([cm.identifier(el, t) for t in "abc"], ["1", None, None])

    def test_missing_power_is_omitted_not_zero(self):
        root = ET.fromstring("<downstream_table><downstream><freq>600000000</freq><pow>N/A</pow><snr>37</snr>"
                             "<mod>256QAM</mod><chid>7</chid></downstream></downstream_table>")
        sampled = {name for name, _, _ in cm.downstream_samples(root)}
        self.assertIn("cablemodem_downstream_snr_db", sampled)
        self.assertNotIn("cablemodem_downstream_power_dbmv", sampled)

    def test_missing_status_is_protocol_error(self):
        with self.assertRaises(client.ModemError):
            cm.system_samples(ET.fromstring("<cm_system_info/>"))

    def test_uptime(self):
        self.assertEqual(client.parse_uptime("1day(s)2h:3m:4s"), 93784)
        self.assertIsNone(client.parse_uptime("soon"))


class RepositoryTests(unittest.TestCase):
    def test_no_real_serial_numbers_or_mac_addresses(self):
        """Fixtures are captured from a real modem; they must stay sanitized."""
        suffixes = {".py", ".md", ".json", ".xml", ".html", ".yml", ".yaml", ".toml", ".example", ".service", ".in"}
        skip = {".git", ".venv", "venv", "build", "dist", "__pycache__"}
        for path in ROOT.rglob("*"):
            if path.suffix not in suffixes or skip & set(path.relative_to(ROOT).parts):
                continue
            text = path.read_text(errors="replace")
            for serial in re.findall(r"\bGD\d{10}\b", text):
                self.assertEqual(serial, "GD0000000000", path)
            for mac in re.findall(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b", text):
                self.assertTrue(mac.startswith("00:00:00:00:00:"), f"{path}: {mac}")


class DashboardTests(unittest.TestCase):
    def test_dashboard(self):
        import make_dashboard

        dashboard = make_dashboard.build_dashboard()
        self.assertEqual(dashboard, make_dashboard.build_dashboard())
        ids = [p["id"] for p in dashboard["panels"]]
        self.assertEqual(len(ids), len(set(ids)))
        exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
        self.assertTrue(all('instance=~"$instance"' in e for e in exprs), exprs)
        self.assertNotIn("$__interval]", json.dumps(dashboard))
        self.assertNotIn("tsunami", json.dumps(dashboard))
        instance = next(v for v in dashboard["templating"]["list"] if v["name"] == "instance")
        self.assertFalse(instance["includeAll"])
        annotations = {a["name"]: a["enable"] for a in dashboard["annotations"]["list"]}
        self.assertFalse(annotations["Lock changes"])

    def test_every_queried_metric_exists(self):
        import make_dashboard

        exposed = {name + ("_total" if m.counter else "") for name, m in cm.METRICS.items()}
        queried = set(re.findall(r"\bcablemodem_\w+", json.dumps(make_dashboard.build_dashboard())))
        self.assertEqual(queried - exposed - {"cablemodem"}, set())

    def test_totals_are_not_summed_from_rolling_windows(self):
        import make_dashboard

        legends = [p["options"]["legend"]["calcs"] for p in make_dashboard.build_dashboard()["panels"]
                   if p["type"] == "timeseries"]
        self.assertFalse([calcs for calcs in legends if "sum" in calcs])

    def test_generated_file_is_current(self):
        import make_dashboard

        self.assertEqual(json.loads((ROOT / "cablemodem-dashboard.json").read_text()),
                         make_dashboard.build_dashboard())


@unittest.skipUnless(shutil.which("promtool"), "promtool (from Prometheus) not installed")
class DashboardQueryTests(unittest.TestCase):
    """Evaluates the dashboard's own PromQL against synthetic series with promtool."""

    INSTANCE = 'job="cablemodem",instance="modem:9884"'

    def query(self, title: str, time_range: str) -> str:
        import make_dashboard

        dashboard = make_dashboard.build_dashboard()
        exprs = {p["title"]: p["targets"][0]["expr"] for p in dashboard["panels"] if p.get("targets")}
        exprs.update({a["name"]: a["expr"] for a in dashboard["annotations"]["list"]})
        return (exprs[title].replace("$job", "cablemodem").replace("$instance", "modem:9884")
                .replace("$__range", time_range))

    def promtool(self, series: list[tuple[str, str]], checks: list[tuple[str, str, list]]):
        test = {"rule_files": [], "tests": [{
            "interval": "30s",
            "input_series": [{"series": s, "values": v} for s, v in series],
            "promql_expr_test": [{"expr": e, "eval_time": t, "exp_samples": samples} for e, t, samples in checks],
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.yml"
            path.write_text(json.dumps(test))  # JSON is valid YAML
            result = subprocess.run(["promtool", "test", "rules", str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_one_event_counts_once_at_any_range(self):
        # Scraped every 30s; the counter goes up once, at 2m.
        one_event = "0 0 0 0 1 1 1 1 1"
        checks = []
        for time_range in ("10m", "1h"):
            checks += [
                (self.query("Events in range by category", time_range), "4m",
                 [{"labels": '{category="reboot"}', "value": 1}]),
                (self.query("Poll errors in range", time_range), "4m",
                 [{"labels": '{kind="transport"}', "value": 1}]),
            ]
        self.promtool([(f'cablemodem_events_total{{{self.INSTANCE},category="reboot"}}', one_event),
                       (f'cablemodem_poll_errors_total{{{self.INSTANCE},kind="transport"}}', one_event)], checks)

    def test_reboot_annotation_sees_a_reboot_after_a_long_outage(self):
        # Up for an hour, offline from 2:30 to 5:00, back at 5:30 with a fresh uptime.
        uptime = (f"cablemodem_uptime_seconds{{{self.INSTANCE}}}", "3600+30x4 _x6 150+30x10")
        self.promtool([uptime], [
            (self.query("Modem reboot", "1h"), "6m",
             [{"labels": f"{{__name__=\"cablemodem_uptime_seconds\",{self.INSTANCE}}}", "value": 180}]),
            (self.query("Modem reboot", "1h"), "2m", []),
            # The previous expression missed it: no 2m window holds samples from both sides of the outage.
            (f"resets(cablemodem_uptime_seconds{{{self.INSTANCE}}}[2m]) > 0", "6m", []),
        ])


if __name__ == "__main__":
    unittest.main()
