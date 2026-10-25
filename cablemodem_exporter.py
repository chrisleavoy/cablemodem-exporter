#!/usr/bin/env python3
"""Prometheus exporter for Compal/CBN cable modems (CD8000 and similar).

Polls the modem every CM_POLL_INTERVAL seconds (rather than on each scrape) and
serves the latest completed poll. Polling on our own timer keeps one web session
alive regardless of how often Prometheus scrapes: every login is written to the
modem's ~20-line event log, so the exporter should log in as rarely as possible.

New modem event log entries are written to stderr at their syslog priority, so
under systemd they land in the journal (journalctl -u cablemodem-exporter).

Configuration is via environment variables; see README.md.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

import cablemodem_client as client
from cablemodem_client import ModemError

__version__ = "0.4.1"

log = logging.getLogger("cablemodem")
modem_events = logging.getLogger("cablemodem.event")


@dataclass(frozen=True)
class Metric:
    help: str
    labels: tuple[str, ...] = ()
    counter: bool = False  # counters are exposed with a _total suffix


CH = ("channel_id", "frequency_mhz")
OFDM = ("receiver", "frequency_mhz")

# Every metric the exporter publishes.
METRICS = {
    # exporter
    "cablemodem_up": Metric("1 if the last poll of the modem succeeded"),
    "cablemodem_last_success_timestamp_seconds": Metric("Time of the last successful poll"),
    "cablemodem_poll_duration_seconds": Metric("Duration of the last poll"),
    "cablemodem_poll_errors": Metric("Failed polls by cause", ("kind",), counter=True),
    "cablemodem_exporter_logins": Metric("Logins made by the exporter", counter=True),
    "cablemodem_exporter_build_info": Metric("Exporter version", ("version",)),
    # modem status
    "cablemodem_info": Metric("Modem identity", ("model", "sw_version", "hw_version", "serial", "docsis_mode")),
    "cablemodem_status": Metric("Modem status string (value always 1)", ("status",)),
    "cablemodem_operational": Metric("1 if the modem reports OPERATIONAL"),
    "cablemodem_network_access": Metric("1 if network access is Allowed"),
    "cablemodem_uptime_seconds": Metric("Modem uptime"),
    # downstream SC-QAM
    "cablemodem_downstream_frequency_hertz": Metric("SC-QAM centre frequency", CH),
    "cablemodem_downstream_power_dbmv": Metric("SC-QAM receive power", CH),
    "cablemodem_downstream_snr_db": Metric("SC-QAM SNR/MER", CH),
    "cablemodem_downstream_locked": Metric("1 if the SC-QAM channel is locked", CH),
    "cablemodem_downstream_modulation": Metric("SC-QAM modulation (value always 1)", CH + ("modulation",)),
    "cablemodem_downstream_channels_locked": Metric("Locked SC-QAM channels"),
    # downstream OFDM (DOCSIS 3.1)
    "cablemodem_ofdm_downstream_plc_power_dbmv": Metric("OFDM PLC power", OFDM),
    "cablemodem_ofdm_downstream_locked": Metric("OFDM lock state per stage (PLC/NCP/MDC1)", OFDM + ("stage",)),
    "cablemodem_ofdm_downstream_fft": Metric("OFDM FFT size (value always 1)", OFDM + ("fft",)),
    # upstream ATDMA
    "cablemodem_upstream_frequency_hertz": Metric("ATDMA centre frequency", CH),
    "cablemodem_upstream_power_dbmv": Metric("ATDMA transmit power", CH),
    "cablemodem_upstream_symbol_rate_symbols_per_second": Metric("ATDMA symbol rate", CH),
    "cablemodem_upstream_bandwidth_hertz": Metric("ATDMA channel width", CH),
    "cablemodem_upstream_modulation": Metric("ATDMA modulation and type (value always 1)",
                                             CH + ("modulation", "channel_type")),
    "cablemodem_upstream_channels": Metric("ATDMA upstream channels"),
    # upstream OFDMA (DOCSIS 3.1)
    "cablemodem_ofdma_upstream_active": Metric("1 if the OFDMA channel is operating", ("channel",)),
    "cablemodem_ofdma_upstream_power_dbmv": Metric("OFDMA reported transmit power", ("channel",)),
    "cablemodem_ofdma_upstream_power_1_6mhz_dbmv": Metric("OFDMA reported power per 1.6 MHz", ("channel",)),
    "cablemodem_ofdma_upstream_bandwidth_hertz": Metric("OFDMA occupied bandwidth", ("channel",)),
    "cablemodem_ofdma_upstream_channel_gain_db": Metric("OFDMA channel gain", ("channel",)),
    "cablemodem_ofdma_upstream_digital_attenuation_db": Metric("OFDMA digital attenuation", ("channel",)),
    # event log
    "cablemodem_events": Metric("New event log entries seen, by category", ("category",), counter=True),
    "cablemodem_events_by_priority": Metric("New event log entries seen, by priority", ("priority",), counter=True),
    "cablemodem_cm_status_events": Metric("CM-STATUS messages by event code and channel",
                                          ("code", "description", "channel_id"), counter=True),
    "cablemodem_last_event_timestamp_seconds": Metric("Time of the newest event seen per category", ("category",)),
    "cablemodem_event_log_entries": Metric("Entries currently in the modem's event log", ("priority",)),
    # Opt-in (CM_EXPORT_EVENT_MESSAGES): one series per distinct log line.
    "cablemodem_event_log_entry_timestamp_seconds": Metric(
        "Entries currently in the modem's event log; value is the event time (0 if logged before the modem "
        "set its clock)", ("time", "priority", "category", "message")),
}

# A sample is (metric name, label values, value).
Sample = tuple[str, tuple[str, ...], float]

POLL_FUNS = (client.FUN_SYSTEM, client.FUN_DS, client.FUN_DS_OFDM, client.FUN_US, client.FUN_US_OFDMA,
             client.FUN_EVENTLOG)
SNR_UNLOCKED = 2147483647  # reported for channels that aren't locked
ERROR_KINDS = ("auth", "protocol", "transport", "internal")

# Syslog priorities; modem priorities outside this list become "other".
SYSLOG_PRIORITIES = {"emergency": 0, "alert": 1, "critical": 2, "error": 3, "warning": 4, "notice": 5,
                     "info": 6, "debug": 7}
OTHER = "other"
PRIORITIES = (*SYSLOG_PRIORITIES, OTHER)

# DOCSIS CM-STATUS event type codes, as in Wireshark's DOCSIS dissector
# (symbol_cm_status_event_vals, packet-docsis-macmgmt.c), which follows the
# CableLabs MULPI spec. 12-15 are reserved. Codes not listed are reported as
# "code N".
CM_STATUS_CODES = {
    1: "Secondary channel MDD timeout",
    2: "QAM/FEC lock failure",
    3: "Sequence out of range",
    4: "MDD recovery",
    5: "QAM/FEC lock recovery",
    6: "T4 timeout",
    7: "T3 retries exceeded",
    8: "Successful ranging after T3 retries exceeded",
    9: "CM operating on battery backup",
    10: "CM returned to AC power",
    11: "MAC removal event",
    16: "DS OFDM profile failure",
    17: "Primary downstream change",
    18: "DPD mismatch",
    19: "Deprecated",
    20: "NCP profile failure",
    21: "PLC failure",
    22: "NCP profile recovery",
    23: "PLC recovery",
    24: "OFDM profile recovery",
    25: "OFDMA failure",
    26: "MAP storage overflow",
    27: "MAP storage almost full",
}
# e.g. "CM-STATUS message sent.  Event Type Code: 23; Chan ID: 34; DSID: N/A; ..."
# The digit limits keep the code/channel labels bounded.
CM_STATUS_RE = re.compile(r"^CM-STATUS message sent\.\s+Event Type Code: (\d{1,2}); Chan ID: (\d{1,3}|N/A);")

# Fixed event categories, first match wins; anything else is "other".
EVENT_CATEGORIES = [
    ("cm_status", r"^CM-STATUS message sent"),
    ("gui_login", r"^GUI Login Status"),
    ("reboot", r"Reboot"),
    ("registration_complete", r"^REGISTRATION COMPLETE"),
    ("mdd_lost", r"Lost MDD"),
    ("mdd_recovery", r"MDD Recovery"),
    ("t4_timeout", r"T4 time.?out"),
    ("t3_timeout", r"T3 time.?out"),
    ("ranging_retries_exhausted", r"Retries exhausted"),
    ("ranging_power", r"^RNG-RSP|Dynamic Range Window"),
    ("sync_failure", r"SYNC Timing Synchronization"),
    ("partial_service", r"Partial Service"),
    ("dhcp", r"DHCP"),
    ("time_of_day", r"ToD request|Time of Day"),
    ("tftp", r"TFTP"),
    ("mimo", r"^MIMO Event"),
    ("dbc", r"DBC-"),
]
CATEGORIES = (*(name for name, _ in EVENT_CATEGORIES), OTHER)


# --------------------------------------------------------------------- parsing

def number(element: ET.Element, tag: str) -> float | None:
    """A numeric field, or None if it's missing, "N/A", inf or NaN."""
    try:
        value = float(element.findtext(tag, "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def identifier(element: ET.Element, tag: str) -> str | None:
    """A channel/receiver number as a canonical label ("01" -> "1"), or None."""
    text = element.findtext(tag, "").strip()
    return str(int(text)) if text.isascii() and text.isdigit() else None


def mhz(hertz: float | None) -> str:
    return f"{hertz / 1e6:g}" if hertz is not None else ""


def parse_event_time(text: str, tz: tzinfo = timezone.utc) -> float | None:
    """Unix time of a log entry, or None if unparseable or logged before the modem set its clock (1970)."""
    try:
        ts = datetime.strptime(text, "%d-%m-%Y %H:%M:%S").replace(tzinfo=tz).timestamp()
    except ValueError:
        return None
    return ts if ts > 946684800 else None  # 2000-01-01


def categorize(message: str) -> str:
    for category, pattern in EVENT_CATEGORIES:
        if re.search(pattern, message, re.IGNORECASE):
            return category
    return OTHER


def cm_status_labels(message: str) -> tuple[str, str, str] | None:
    """(code, description, channel_id) for a CM-STATUS line."""
    match = CM_STATUS_RE.match(message)
    if not match:
        return None
    code = int(match.group(1))
    channel = "none" if match.group(2) == "N/A" else str(int(match.group(2)))
    return str(code), CM_STATUS_CODES.get(code, f"code {code}"), channel


def parse_events(root: ET.Element) -> list[tuple[str, str, str]]:
    """(time, priority, message) for each log entry, oldest first."""
    entries = []
    for entry in root.iter("eventlog"):
        priority = entry.findtext("prior", "").strip().lower()
        message = entry.findtext("text", "").split(";CM-MAC=")[0]  # drop the MAC address suffix
        entries.append((entry.findtext("time", "").strip(),
                        priority if priority in SYSLOG_PRIORITIES else OTHER,
                        " ".join(message.split())))
    return entries


def system_samples(root: ET.Element) -> list[Sample]:
    status = root.findtext("cm_status", "").strip()
    if not status:
        raise ModemError("protocol", "system info has no cm_status")
    identity = tuple(root.findtext(tag, "").strip() for tag in
                     ("HwModel", "SwVersion", "cm_hardware_version", "cm_serial_number", "cm_docsis_mode"))
    samples: list[Sample] = [("cablemodem_info", identity, 1), ("cablemodem_status", (status,), 1),
                             ("cablemodem_operational", (), status == "OPERATIONAL")]
    access = root.findtext("cm_network_access", "").strip()
    if access:
        samples.append(("cablemodem_network_access", (), access == "Allowed"))
    uptime = client.parse_uptime(root.findtext("cm_system_uptime", ""))
    if uptime is not None:
        samples.append(("cablemodem_uptime_seconds", (), uptime))
    return samples


def downstream_samples(root: ET.Element) -> list[Sample]:
    samples: list[Sample] = []
    locked_count = 0
    for channel in root.iter("downstream"):
        channel_id, hertz = identifier(channel, "chid"), number(channel, "freq")
        if channel_id is None or hertz is None:
            continue
        labels = (channel_id, mhz(hertz))
        modulation = channel.findtext("mod", "").strip()
        snr, power = number(channel, "snr"), number(channel, "pow")
        locked = snr not in (None, SNR_UNLOCKED) and modulation not in ("", "unknown")
        locked_count += locked
        samples += [("cablemodem_downstream_frequency_hertz", labels, hertz),
                    ("cablemodem_downstream_locked", labels, locked),
                    ("cablemodem_downstream_modulation", labels + (modulation,), 1)]
        if locked:
            samples.append(("cablemodem_downstream_snr_db", labels, snr))
            if power is not None:
                samples.append(("cablemodem_downstream_power_dbmv", labels, power))
    samples.append(("cablemodem_downstream_channels_locked", (), locked_count))
    return samples


def downstream_ofdm_samples(root: ET.Element) -> list[Sample]:
    samples: list[Sample] = []
    for receiver in root.iter("downstream"):
        receiver_id, fft = identifier(receiver, "Receiver"), receiver.findtext("FFTType", "").strip()
        if receiver_id is None or fft in ("", "N/A"):
            continue
        labels = (receiver_id, mhz(number(receiver, "Subcarr0Frequency")))
        samples.append(("cablemodem_ofdm_downstream_fft", labels + (fft,), 1))
        power = number(receiver, "PLCPower")
        if power is not None:
            samples.append(("cablemodem_ofdm_downstream_plc_power_dbmv", labels, power))
        for stage in ("PLC", "NCP", "MDC1"):
            state = receiver.findtext(f"{stage}Locked")
            if state is not None:
                samples.append(("cablemodem_ofdm_downstream_locked", labels + (stage,), state.strip() == "YES"))
    return samples


def upstream_samples(root: ET.Element) -> list[Sample]:
    samples: list[Sample] = []
    count = 0
    for channel in root.iter("upstream"):
        channel_id, hertz = identifier(channel, "usid"), number(channel, "freq")
        if channel_id is None or hertz is None:
            continue
        count += 1
        labels = (channel_id, mhz(hertz))
        samples += [("cablemodem_upstream_frequency_hertz", labels, hertz),
                    ("cablemodem_upstream_modulation", labels + (channel.findtext("mod", "").strip(),
                                                                 channel.findtext("channeltype", "").strip()), 1)]
        power, width, msps = number(channel, "power"), number(channel, "bandwidth"), number(channel, "srate")
        if power is not None:
            samples.append(("cablemodem_upstream_power_dbmv", labels, power))
        if width is not None:
            samples.append(("cablemodem_upstream_bandwidth_hertz", labels, width))
        if msps is not None:  # reported in Msym/s, e.g. 5.120 on a 6.4 MHz channel
            samples.append(("cablemodem_upstream_symbol_rate_symbols_per_second", labels, msps * 1e6))
    samples.append(("cablemodem_upstream_channels", (), count))
    return samples


def upstream_ofdma_samples(root: ET.Element) -> list[Sample]:
    samples: list[Sample] = []
    for channel in root.iter("upstream"):
        channel_id = identifier(channel, "CH")
        if channel_id is None:
            continue
        active = channel.findtext("STATE", "").strip() == "OPERATE"
        samples.append(("cablemodem_ofdma_upstream_active", (channel_id,), active))
        if not active:
            continue
        for name, tag, scale in (("cablemodem_ofdma_upstream_power_dbmv", "repPower", 1),
                                 ("cablemodem_ofdma_upstream_power_1_6mhz_dbmv", "repPower1_6", 1),
                                 ("cablemodem_ofdma_upstream_channel_gain_db", "ChannelGain", 1),
                                 ("cablemodem_ofdma_upstream_digital_attenuation_db", "DigitalAtt", 1),
                                 ("cablemodem_ofdma_upstream_bandwidth_hertz", "BW", 1e6)):  # BW is in MHz
            value = number(channel, tag)
            if value is not None:
                samples.append((name, (channel_id,), value * scale))
    return samples


def event_log_samples(entries: list[tuple[str, str, str]], tz: tzinfo, with_messages: bool) -> list[Sample]:
    samples: list[Sample] = [("cablemodem_event_log_entries", (p,), sum(e[1] == p for e in entries))
                             for p in PRIORITIES]
    if with_messages:
        for time_text, priority, message in dict.fromkeys(entries):
            samples.append(("cablemodem_event_log_entry_timestamp_seconds",
                            (time_text, priority, categorize(message), message), parse_event_time(time_text, tz) or 0))
    return samples


# ---------------------------------------------------------------------- events

class EventLogTracker:
    """Finds entries that are new since the last poll.

    The modem's log is a ~20-line FIFO, so an entry is new if it appears more
    times than in the previous poll's copy (counting, so that repeated identical
    lines such as the 1970-dated reboot message aren't swallowed). The previous
    copy is saved to disk so a restart doesn't report the same entries again.
    """

    def __init__(self, snapshot_path: Path):
        self.snapshot_path = snapshot_path
        self.previous: list[tuple[str, str, str]] = self._load()

    def _load(self) -> list[tuple[str, str, str]]:
        try:
            data = json.loads(self.snapshot_path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable %s: %s", self.snapshot_path, e)
            return []
        if not (isinstance(data, list) and all(isinstance(e, list) and len(e) == 3
                                               and all(isinstance(f, str) for f in e) for e in data)):
            log.warning("ignoring malformed %s", self.snapshot_path)
            return []
        return [tuple(e) for e in data]

    def new_entries(self, entries: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        remaining = list(self.previous)
        new = []
        for entry in entries:
            if entry in remaining:
                remaining.remove(entry)
            else:
                new.append(entry)
        self.previous = entries
        try:
            tmp = self.snapshot_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(entries))
            tmp.replace(self.snapshot_path)
        except OSError as e:
            log.error("could not save %s (a restart may report these events again): %s", self.snapshot_path, e)
        return new


# -------------------------------------------------------------------- exporter

class Exporter:
    """Polls the modem and serves the latest completed poll to Prometheus.

    Each poll builds a complete tuple of samples and publishes it with a single
    assignment, so a scrape always sees exactly one poll, and anything that poll
    didn't read (a missing field, or everything after a failure) is absent
    rather than left over from an earlier poll.
    """

    def __init__(self, modem: client.Modem, tracker: EventLogTracker, tz: tzinfo = timezone.utc,
                 export_event_messages: bool = False):
        self.modem = modem
        self.tracker = tracker
        self.tz = tz
        self.export_event_messages = export_event_messages
        # Counter values by (metric, labels); every fixed label value starts at 0.
        self.counters: dict[tuple[str, tuple[str, ...]], float] = {("cablemodem_exporter_logins", ()): 0}
        for name, values in (("cablemodem_poll_errors", ERROR_KINDS), ("cablemodem_events", CATEGORIES),
                             ("cablemodem_events_by_priority", PRIORITIES)):
            for value in values:
                self.counters[(name, (value,))] = 0
        self.newest_event: dict[str, float] = {}
        self.last_success: float | None = None
        self.samples: tuple[Sample, ...] = self._exporter_samples(up=False, duration=None)

    def describe(self) -> list:
        return []  # don't let the registry call collect() at registration

    def collect(self) -> list:
        samples = self.samples  # one completed poll; replaced, never modified
        families: dict[str, GaugeMetricFamily | CounterMetricFamily] = {}
        for name, labels, value in samples:
            if name not in families:
                metric = METRICS[name]
                family_type = CounterMetricFamily if metric.counter else GaugeMetricFamily
                families[name] = family_type(name, metric.help, labels=metric.labels)
            families[name].add_metric(labels, value)
        return list(families.values())

    def poll(self) -> None:
        start = time.monotonic()
        logins_before = self.modem.logins
        samples: list[Sample] = []
        entries = None
        error = None
        try:
            roots = {fun: self.modem.get(fun) for fun in POLL_FUNS}
            samples = (system_samples(roots[client.FUN_SYSTEM])
                       + downstream_samples(roots[client.FUN_DS])
                       + downstream_ofdm_samples(roots[client.FUN_DS_OFDM])
                       + upstream_samples(roots[client.FUN_US])
                       + upstream_ofdma_samples(roots[client.FUN_US_OFDMA]))
            entries = parse_events(roots[client.FUN_EVENTLOG])
            samples += event_log_samples(entries, self.tz, self.export_event_messages)
        except ModemError as e:
            log.warning("poll failed: %s", e)
            error, samples, entries = e.kind, [], None
        except Exception:
            log.exception("unexpected error while polling")
            error, samples, entries = "internal", [], None

        new_events = self.tracker.new_entries(entries) if entries is not None else []
        self._count_events(new_events)
        self.counters[("cablemodem_exporter_logins", ())] += self.modem.logins - logins_before
        if error is None:
            self.last_success = time.time()
        else:
            self.counters[("cablemodem_poll_errors", (error,))] += 1
        self.samples = self._exporter_samples(error is None, time.monotonic() - start) + tuple(
            (name, labels, float(value)) for name, labels, value in samples)
        self._journal(new_events)

    def _exporter_samples(self, up: bool, duration: float | None) -> tuple[Sample, ...]:
        samples: list[Sample] = [("cablemodem_up", (), float(up)),
                                 ("cablemodem_exporter_build_info", (__version__,), 1.0)]
        if duration is not None:
            samples.append(("cablemodem_poll_duration_seconds", (), duration))
        if self.last_success is not None:
            samples.append(("cablemodem_last_success_timestamp_seconds", (), self.last_success))
        samples += [(name, labels, value) for (name, labels), value in self.counters.items()]
        samples += [("cablemodem_last_event_timestamp_seconds", (category,), ts)
                    for category, ts in sorted(self.newest_event.items())]
        return tuple(samples)

    def _count_events(self, new: list[tuple[str, str, str]]) -> None:
        for time_text, priority, message in new:
            category = categorize(message)
            self.counters[("cablemodem_events", (category,))] += 1
            self.counters[("cablemodem_events_by_priority", (priority,))] += 1
            status = cm_status_labels(message)
            if status:
                key = ("cablemodem_cm_status_events", status)
                self.counters[key] = self.counters.get(key, 0) + 1
            ts = parse_event_time(time_text, self.tz)
            if ts is not None:
                self.newest_event[category] = max(ts, self.newest_event.get(category, ts))

    def _journal(self, new: list[tuple[str, str, str]]) -> None:
        """Write each new event to the journal at the modem's syslog priority."""
        for time_text, priority, message in new:
            modem_events.info("modem event %s [%s] %s: %s", time_text, priority, categorize(message), message,
                              extra={"syslog_priority": SYSLOG_PRIORITIES.get(priority, 5)})


# ------------------------------------------------------------------------ main

class JournalFormatter(logging.Formatter):
    """Prefixes each line with <N> so journald records it at that syslog priority."""

    LEVELS = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3, logging.CRITICAL: 2}

    def format(self, record: logging.LogRecord) -> str:
        priority = getattr(record, "syslog_priority", self.LEVELS.get(record.levelno, 6))
        return f"<{priority}>{super().format(record)}"


def setup_logging() -> None:
    handler = logging.StreamHandler()
    if os.environ.get("JOURNAL_STREAM"):  # set by systemd when stderr goes to the journal
        handler.setFormatter(JournalFormatter("%(message)s"))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def env_flag(name: str) -> bool:
    value = os.environ.get(name, "false").lower()
    if value not in ("true", "false", "1", "0", "yes", "no"):
        raise SystemExit(f"{name} must be true or false, got {value!r}")
    return value in ("true", "1", "yes")


def main() -> None:
    setup_logging()
    modem = client.modem_from_env()
    interval = client.env_float("CM_POLL_INTERVAL", 30)
    port_text = os.environ.get("CM_LISTEN_PORT", "9884")
    if not (port_text.isdigit() and 1 <= int(port_text) <= 65535):
        raise SystemExit(f"CM_LISTEN_PORT must be 1-65535, got {port_text!r}")
    try:
        tz = ZoneInfo(os.environ.get("CM_EVENT_TIMEZONE", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit(f"CM_EVENT_TIMEZONE: unknown time zone {os.environ['CM_EVENT_TIMEZONE']!r}") from None
    state_dir = Path(os.environ.get("CM_STATE_DIR") or os.environ.get("STATE_DIRECTORY") or "/var/lib/cablemodem")
    state_dir.mkdir(parents=True, exist_ok=True)

    exporter = Exporter(modem, EventLogTracker(state_dir / "eventlog_snapshot.json"), tz,
                        env_flag("CM_EXPORT_EVENT_MESSAGES"))
    REGISTRY.register(exporter)
    address = os.environ.get("CM_LISTEN_ADDRESS", "127.0.0.1")
    server, _ = start_http_server(int(port_text), addr=address)
    log.info("cablemodem-exporter %s listening on %s:%s, polling every %gs", __version__, address, port_text,
             interval)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        exporter.poll()
        stop.wait(interval)
    server.shutdown()
    modem.close()
    log.info("stopped")


if __name__ == "__main__":
    main()
