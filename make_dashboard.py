#!/usr/bin/env python3
"""Generates the Grafana dashboard. Run: python3 make_dashboard.py > cablemodem-dashboard.json"""

from __future__ import annotations

import json

DS = {"type": "prometheus", "uid": "${DS}"}
SEL = 'job=~"$job",instance=~"$instance"'
WARN_PRIORITIES = "warning|error|critical|alert|emergency"
SCRAPE_INTERVAL = "30s"


def thresholds(*steps: tuple[str, float | None]) -> dict:
    """steps: (color, value) with the first value None (base)."""
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}


SNR_TH = thresholds(("red", None), ("orange", 30), ("green", 33))
DS_PWR_TH = thresholds(("red", None), ("orange", -15), ("yellow", -7), ("green", -5), ("yellow", 5),
                       ("orange", 7), ("red", 15))
US_PWR_TH = thresholds(("red", None), ("yellow", 35), ("green", 38), ("yellow", 48), ("orange", 51), ("red", 53))
CH_LEGEND = "ch {{channel_id}} · {{frequency_mhz}} MHz"


def target(expr: str, legend: str = "", instant: bool = False, fmt: str = "time_series", ref: str = "A") -> dict:
    t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref}
    if instant:
        t.update({"instant": True, "range": False})
    if fmt != "time_series":
        t["format"] = fmt
    return t


class DashboardBuilder:
    def __init__(self) -> None:
        self.panels: list[dict] = []
        self._next_id = 0
        self.y = 0

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def advance(self, height: int) -> None:
        self.y += height

    def row(self, title: str) -> None:
        self.panels.append({"type": "row", "title": title, "collapsed": False, "id": self._id(),
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}, "panels": []})
        self.y += 1

    def panel(self, kind: str, title: str, x: int, w: int, h: int, targets: list[dict], unit: str | None = None,
              desc: str | None = None, th: dict | None = None, decimals: int | None = None,
              options: dict | None = None, custom: dict | None = None, overrides: list | None = None,
              transformations: list | None = None, mappings: list | None = None,
              minv: float | None = None, maxv: float | None = None, interval: str | None = None) -> dict:
        defaults: dict = {}
        if unit:
            defaults["unit"] = unit
        if th:
            defaults["thresholds"] = th
            defaults["color"] = {"mode": "thresholds"}
        if decimals is not None:
            defaults["decimals"] = decimals
        if custom:
            defaults["custom"] = custom
        if mappings:
            defaults["mappings"] = mappings
        if minv is not None:
            defaults["min"] = minv
        if maxv is not None:
            defaults["max"] = maxv
        p = {"type": kind, "title": title, "id": self._id(), "datasource": DS,
             "gridPos": {"h": h, "w": w, "x": x, "y": self.y}, "targets": targets,
             "fieldConfig": {"defaults": defaults, "overrides": overrides or []}}
        if desc:
            p["description"] = desc
        if options:
            p["options"] = options
        if transformations:
            p["transformations"] = transformations
        if interval:
            p["interval"] = interval
        self.panels.append(p)
        return p

    def stat(self, title: str, x: int, w: int, expr: str, unit: str | None = None, th: dict | None = None,
             desc: str | None = None, decimals: int | None = None, mappings: list | None = None,
             color_mode: str = "value") -> dict:
        return self.panel("stat", title, x, w, 4, [target(expr, instant=True)], unit=unit, th=th, desc=desc,
                          decimals=decimals, mappings=mappings,
                          options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                                   "colorMode": color_mode, "graphMode": "none", "textMode": "auto",
                                   "justifyMode": "center"})

    def timeseries(self, title: str, x: int, w: int, h: int, targets: list[dict], unit: str | None = None,
                   th: dict | None = None, desc: str | None = None, minv: float | None = None,
                   maxv: float | None = None, legend_calcs: tuple[str, ...] = ("min", "mean", "max", "lastNotNull"),
                   draw: str = "line", stack: bool = False, interval: str | None = None) -> dict:
        custom = {"drawStyle": draw, "lineWidth": 1, "fillOpacity": 0 if draw == "line" else 80,
                  "showPoints": "never", "spanNulls": False, "axisSoftMin": minv, "axisSoftMax": maxv,
                  "thresholdsStyle": {"mode": "line+area" if th else "off"}}
        if stack:
            custom["stacking"] = {"mode": "normal", "group": "A"}
        p = self.panel("timeseries", title, x, w, h, targets, unit=unit, th=th, desc=desc, custom=custom,
                       interval=interval,
                       options={"legend": {"displayMode": "table", "placement": "right", "calcs": list(legend_calcs),
                                           "sortBy": "Min" if "min" in legend_calcs else None, "sortDesc": False},
                                "tooltip": {"mode": "multi", "sort": "asc"}})
        # Thresholds drive the background bands, not the line colours.
        p["fieldConfig"]["defaults"]["color"] = {"mode": "palette-classic"}
        return p


def add_overview(b: DashboardBuilder) -> None:
    b.row("Overview")
    b.stat("Modem status", 0, 3, f"cablemodem_operational{{{SEL}}}",
           mappings=[{"type": "value", "options": {"1": {"text": "Operational", "color": "green"},
                                                   "0": {"text": "Not operational", "color": "red"}}}],
           color_mode="background")
    b.stat("Uptime", 3, 3, f"cablemodem_uptime_seconds{{{SEL}}}", unit="s", decimals=0,
           th=thresholds(("orange", None), ("green", 3600)))
    b.stat("DS channels locked", 6, 3, f"cablemodem_downstream_channels_locked{{{SEL}}}",
           th=thresholds(("red", None), ("orange", 24), ("green", 30)), desc="Locked SC-QAM downstream channels")
    b.stat("US channels", 9, 2, f"cablemodem_upstream_channels{{{SEL}}}",
           th=thresholds(("red", None), ("orange", 3), ("green", 4)))
    b.stat("Min DS SNR", 11, 3, f"min(cablemodem_downstream_snr_db{{{SEL}}})", unit="dB", th=SNR_TH,
           desc="Worst SC-QAM channel right now. 256-QAM wants ≥33 dB.")
    b.stat("DS power spread", 14, 3,
           f"max(cablemodem_downstream_power_dbmv{{{SEL}}}) - min(cablemodem_downstream_power_dbmv{{{SEL}}})",
           unit="dB", decimals=1, th=thresholds(("green", None), ("yellow", 6), ("red", 10)),
           desc="Difference between hottest and weakest downstream channel; a large tilt suggests a "
                "cabling/splitter issue.")
    b.stat("Max US power", 17, 3, f"max(cablemodem_upstream_power_dbmv{{{SEL}}})", unit="dBmV", th=US_PWR_TH,
           desc="Highest ATDMA transmit power. Above ~51 dBmV the modem is straining to reach the CMTS.")
    b.stat("Warnings in range", 20, 4,
           f'sum(increase(cablemodem_events_by_priority_total{{{SEL},priority=~"{WARN_PRIORITIES}"}}[$__range]))',
           decimals=0, th=thresholds(("green", None), ("orange", 1), ("red", 10)),
           desc="Warning-or-worse modem log entries observed by the exporter during the selected range")
    b.advance(4)


def add_downstream(b: DashboardBuilder) -> None:
    b.row("Downstream SC-QAM")
    b.timeseries("Downstream SNR per channel", 0, 24, 10,
                 [target(f"cablemodem_downstream_snr_db{{{SEL}}}", CH_LEGEND)], unit="dB", th=SNR_TH,
                 desc="SNR/MER per locked SC-QAM channel. Red band <30 dB, orange 30–33 dB.", minv=28, maxv=42)
    b.advance(10)
    b.timeseries("Downstream SNR – worst / mean / best", 0, 12, 8,
                 [target(f"min(cablemodem_downstream_snr_db{{{SEL}}})", "worst", ref="A"),
                  target(f"avg(cablemodem_downstream_snr_db{{{SEL}}})", "mean", ref="B"),
                  target(f"max(cablemodem_downstream_snr_db{{{SEL}}})", "best", ref="C")],
                 unit="dB", th=SNR_TH, minv=28, maxv=42, legend_calcs=("min", "mean", "max"))
    b.panel("bargauge", "Current SNR by frequency", 12, 12, 8,
            [target(f"cablemodem_downstream_snr_db{{{SEL}}}", instant=True, fmt="table")],
            unit="dB", th=SNR_TH, minv=25, maxv=45,
            # One bar per row, named by its first string field; DS frequencies are all
            # three digits so a string sort is a frequency sort.
            transformations=[
                {"id": "organize", "options": {"excludeByName": {"Time": True, "__name__": True, "instance": True,
                                                                 "job": True, "channel_id": True}}},
                {"id": "sortBy", "options": {"sort": [{"field": "frequency_mhz"}]}}],
            options={"orientation": "vertical", "displayMode": "gradient", "showUnfilled": True,
                     "valueMode": "text", "namePlacement": "auto", "sizing": "auto",
                     "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/^Value$/", "values": True}})
    b.advance(8)
    b.timeseries("Downstream power per channel", 0, 24, 10,
                 [target(f"cablemodem_downstream_power_dbmv{{{SEL}}}", CH_LEGEND)], unit="dBmV", th=DS_PWR_TH,
                 desc="Receive level per SC-QAM channel. Ideal −7…+7 dBmV; beyond ±15 the modem struggles.",
                 minv=-10, maxv=10)
    b.advance(10)
    b.timeseries("Locked downstream channels", 0, 12, 6,
                 [target(f"cablemodem_downstream_channels_locked{{{SEL}}}", "SC-QAM locked", ref="A"),
                  target(f'sum(cablemodem_ofdm_downstream_locked{{{SEL},stage="MDC1"}})', "OFDM locked", ref="B")],
                 legend_calcs=("min", "lastNotNull"))
    b.panel("table", "Downstream channels now", 12, 12, 6,
            [target(f"cablemodem_downstream_snr_db{{{SEL}}}", instant=True, fmt="table", ref="A"),
             target(f"cablemodem_downstream_power_dbmv{{{SEL}}}", instant=True, fmt="table", ref="B")],
            transformations=[
                {"id": "merge", "options": {}},
                {"id": "organize", "options": {
                    "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True},
                    "renameByName": {"channel_id": "Channel", "frequency_mhz": "MHz", "Value #A": "SNR (dB)",
                                     "Value #B": "Power (dBmV)"},
                    "indexByName": {"channel_id": 0, "frequency_mhz": 1, "Value #A": 2, "Value #B": 3}}},
                {"id": "convertFieldType",
                 "options": {"conversions": [{"targetField": "MHz", "destinationType": "number"}]}},
                {"id": "sortBy", "options": {"sort": [{"field": "MHz"}]}}],
            overrides=[{"matcher": {"id": "byName", "options": "SNR (dB)"},
                        "properties": [{"id": "thresholds", "value": SNR_TH},
                                       {"id": "custom.cellOptions",
                                        "value": {"type": "color-background", "mode": "basic"}}]},
                       {"matcher": {"id": "byName", "options": "Power (dBmV)"},
                        "properties": [{"id": "thresholds", "value": DS_PWR_TH}, {"id": "decimals", "value": 1},
                                       {"id": "custom.cellOptions", "value": {"type": "color-text"}}]}],
            options={"showHeader": True, "cellHeight": "sm"})
    b.advance(6)


def add_ofdm(b: DashboardBuilder) -> None:
    b.row("Downstream OFDM (DOCSIS 3.1)")
    b.timeseries("OFDM PLC power", 0, 12, 7,
                 [target(f"cablemodem_ofdm_downstream_plc_power_dbmv{{{SEL}}}",
                         "rx {{receiver}} · {{frequency_mhz}} MHz")],
                 unit="dBmV", th=DS_PWR_TH, minv=-10, maxv=10)
    b.panel("state-timeline", "OFDM lock state", 12, 12, 7,
            [target(f"cablemodem_ofdm_downstream_locked{{{SEL}}}", "rx {{receiver}} {{stage}}")],
            mappings=[{"type": "value", "options": {"1": {"text": "locked", "color": "green"},
                                                    "0": {"text": "unlocked", "color": "red"}}}],
            th=thresholds(("red", None), ("green", 1)),
            options={"showValue": "never", "mergeValues": True, "rowHeight": 0.8, "alignValue": "left",
                     "legend": {"showLegend": False}})
    b.advance(7)


def add_upstream(b: DashboardBuilder) -> None:
    b.row("Upstream")
    b.timeseries("Upstream ATDMA transmit power", 0, 12, 8,
                 [target(f"cablemodem_upstream_power_dbmv{{{SEL}}}", CH_LEGEND)], unit="dBmV", th=US_PWR_TH,
                 desc="Higher means more attenuation between modem and CMTS. ~38–48 dBmV is comfortable.",
                 minv=34, maxv=54)
    b.timeseries("Upstream OFDMA power", 12, 12, 8,
                 [target(f"cablemodem_ofdma_upstream_power_dbmv{{{SEL}}}", "ch {{channel}} total", ref="A"),
                  target(f"cablemodem_ofdma_upstream_power_1_6mhz_dbmv{{{SEL}}}", "ch {{channel}} per 1.6 MHz",
                         ref="B")],
                 unit="dBmV")
    b.advance(8)


# Totals ("in range") are always one instant increase(...[$__range]). Summing the
# points of a rolling increase() would count each event once per window it falls
# in, so graphs over time show rates instead.

def add_events(b: DashboardBuilder) -> None:
    b.row("Event log")
    p = b.timeseries("Event rate by category", 0, 10, 8,
                     [target(f"sum by (category) (rate(cablemodem_events_total{{{SEL}}}[$__rate_interval])) * 3600"
                             " > 0", "{{category}}")],
                     draw="bars", stack=True, legend_calcs=("mean", "max"), interval=SCRAPE_INTERVAL,
                     desc="New modem log entries per hour (a rolling rate, so one event spreads over a few bars). "
                          "Best effort: entries that scroll out of the modem's ~20-line log between polls are "
                          "never seen.")
    p["fieldConfig"]["defaults"]["custom"]["axisLabel"] = "events / hour"
    b.panel("bargauge", "Events in range by category", 10, 6, 8,
            [target(f"sum by (category) (increase(cablemodem_events_total{{{SEL}}}[$__range])) > 0",
                    "{{category}}", instant=True)],
            decimals=0, desc="New modem log entries seen during the selected time range (estimated from counter "
                             "samples, like any increase())",
            options={"orientation": "horizontal", "displayMode": "basic", "showUnfilled": False,
                     "valueMode": "text", "namePlacement": "left",
                     "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}})
    b.panel("table", "CM-STATUS events by channel (in range)", 16, 8, 8,
            [target(f"sum by (channel_id, description) "
                    f"(increase(cablemodem_cm_status_events_total{{{SEL}}}[$__range])) > 0",
                    instant=True, fmt="table")],
            transformations=[{"id": "organize", "options": {
                "excludeByName": {"Time": True},
                "renameByName": {"channel_id": "Channel", "description": "Event", "Value": "Count"}}},
                {"id": "sortBy", "options": {"sort": [{"field": "Count", "desc": True}]}}],
            decimals=0, options={"showHeader": True, "cellHeight": "sm"})
    b.advance(8)
    b.panel("table", "Modem event log", 0, 24, 12,
            [target(f"max by (time, priority, category, message) "
                    f"(last_over_time(cablemodem_event_log_entry_timestamp_seconds{{{SEL}}}[$__range])) * 1000",
                    instant=True, fmt="table")],
            desc="Every log entry seen during the selected range, newest first. Needs the exporter's opt-in "
                 "CM_EXPORT_EVENT_MESSAGES=true (one series per distinct log line). The exporter also writes "
                 "every new entry to the systemd journal (journalctl -u cablemodem-exporter).",
            transformations=[
                {"id": "organize", "options": {
                    "excludeByName": {"Time": True, "time": True},
                    "renameByName": {"Value": "Time", "priority": "Priority", "category": "Category",
                                     "message": "Message"},
                    "indexByName": {"Value": 0, "priority": 1, "category": 2, "message": 3}}},
                {"id": "convertFieldType",
                 "options": {"conversions": [{"targetField": "Time", "destinationType": "time"}]}},
                {"id": "sortBy", "options": {"sort": [{"field": "Time", "desc": True}]}}],
            overrides=[{"matcher": {"id": "byName", "options": "Time"},
                        "properties": [{"id": "custom.width", "value": 180},
                                       {"id": "unit", "value": "dateTimeAsLocal"}]},
                       {"matcher": {"id": "byName", "options": "Category"},
                        "properties": [{"id": "custom.width", "value": 170}]},
                       {"matcher": {"id": "byName", "options": "Priority"},
                        "properties": [{"id": "custom.width", "value": 100},
                                       {"id": "mappings", "value": [{"type": "value", "options": {
                                           "notice": {"color": "text"}, "warning": {"color": "orange"},
                                           "error": {"color": "red"}, "critical": {"color": "red"}}}]},
                                       {"id": "custom.cellOptions", "value": {"type": "color-text"}}]}],
            options={"showHeader": True, "cellHeight": "sm", "footer": {"show": False}})
    b.advance(12)


def add_exporter_health(b: DashboardBuilder) -> None:
    b.row("Exporter health")
    b.stat("Last poll", 0, 4, f"cablemodem_up{{{SEL}}}",
           mappings=[{"type": "value", "options": {"1": {"text": "OK", "color": "green"},
                                                   "0": {"text": "Failing", "color": "red"}}}],
           color_mode="background")
    b.stat("Last successful poll", 4, 4, f"time() - cablemodem_last_success_timestamp_seconds{{{SEL}}}",
           unit="s", decimals=0, th=thresholds(("green", None), ("orange", 120), ("red", 300)),
           desc="Seconds since the exporter last read the modem successfully")
    b.stat("Exporter logins", 8, 4, f"increase(cablemodem_exporter_logins_total{{{SEL}}}[$__range])",
           decimals=0, th=thresholds(("green", None), ("orange", 5)),
           desc="Logins the exporter made in this range. Each one writes a 'GUI Login' line to the modem's "
                "~20-entry log, so this should stay near zero.")
    b.panel("stat", "Poll errors in range", 12, 12, 4,
            [target(f"sum by (kind) (increase(cablemodem_poll_errors_total{{{SEL}}}[$__range]))",
                    "{{kind}}", instant=True)],
            decimals=0, th=thresholds(("green", None), ("orange", 1), ("red", 10)),
            desc="Failed polls during the selected time range, by cause",
            options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                     "colorMode": "value", "graphMode": "none", "textMode": "value_and_name",
                     "justifyMode": "center"})
    b.advance(4)
    b.timeseries("Poll duration", 0, 24, 5,
                 [target(f"cablemodem_poll_duration_seconds{{{SEL}}}", "poll")], unit="s",
                 legend_calcs=("mean", "max"))
    b.advance(5)


def build_dashboard() -> dict:
    b = DashboardBuilder()
    add_overview(b)
    add_downstream(b)
    add_ofdm(b)
    add_upstream(b)
    add_events(b)
    add_exporter_health(b)

    # One modem at a time: summaries such as min SNR or power spread would
    # otherwise mix channels from different modems.
    def variable(name: str, label: str, query: str) -> dict:
        return {"name": name, "label": label, "type": "query", "datasource": DS, "query": query,
                "definition": query, "refresh": 2, "includeAll": False, "multi": False, "current": {},
                "sort": 1, "hide": 0}

    return {
        "uid": "cablemodem",
        "title": "Cable Modem",
        "description": "DOCSIS cable modem channels, SNR, power and event log from cablemodem-exporter",
        "tags": ["cablemodem", "network", "docsis"],
        "timezone": "browser",
        "refresh": "1m",
        "time": {"from": "now-24h", "to": "now"},
        "schemaVersion": 39,
        "editable": True,
        "graphTooltip": 1,
        "templating": {"list": [
            {"name": "DS", "label": "Prometheus", "type": "datasource", "query": "prometheus", "current": {},
             "hide": 0},
            variable("job", "Job", "label_values(cablemodem_up, job)"),
            variable("instance", "Instance", 'label_values(cablemodem_up{job=~"$job"}, instance)'),
        ]},
        "annotations": {"list": [
            # Marks the modem's first 10 minutes after a boot. A reboot takes it offline for
            # minutes, so there are no uptime samples on both sides of a short window for
            # resets() to compare; a low uptime is visible however long the outage was.
            {"name": "Modem reboot", "datasource": DS, "enable": True, "iconColor": "red",
             "expr": f"cablemodem_uptime_seconds{{{SEL}}} < 600", "step": "60s",
             "titleFormat": "Modem rebooted", "textFormat": ""},
            {"name": "Warning events", "datasource": DS, "enable": True, "iconColor": "orange",
             "expr": f'sum by (priority) (increase(cablemodem_events_by_priority_total'
                     f'{{{SEL},priority=~"{WARN_PRIORITIES}"}}[2m])) > 0',
             "step": "60s", "titleFormat": "{{priority}} event(s) in modem log", "textFormat": ""},
            # Off by default: a flapping channel produces hundreds of these a day.
            {"name": "Lock changes", "datasource": DS, "enable": False, "iconColor": "yellow",
             "expr": f"sum by (description, channel_id) (increase(cablemodem_cm_status_events_total{{{SEL}}}[2m])) > 0",
             "step": "60s", "titleFormat": "{{description}} (ch {{channel_id}})", "textFormat": ""},
        ]},
        "panels": b.panels,
    }


def main() -> None:
    print(json.dumps(build_dashboard(), indent=2))


if __name__ == "__main__":
    main()
