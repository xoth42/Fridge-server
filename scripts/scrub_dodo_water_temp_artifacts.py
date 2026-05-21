#!/usr/bin/env python3
"""Find and optionally delete Dodo water temperature zero-conversion artifacts.

The Dodo uploader historically converted raw 0 readings on the cooling-water
temperature channels from Fahrenheit to Celsius, producing values near
-17.78 C on the pushed Prometheus series.

This script queries Prometheus for the affected series, groups contiguous bad
samples into windows, and can delete those windows through the Prometheus TSDB
admin API.

Examples:

  Dry run for one day:
    python3 scripts/scrub_dodo_water_temp_artifacts.py \
      --start 2026-05-01T00:00:00Z \
      --end 2026-05-02T00:00:00Z

  Delete the detected windows and clean tombstones:
    python3 scripts/scrub_dodo_water_temp_artifacts.py \
      --start 2026-05-01T00:00:00Z \
      --end 2026-05-02T00:00:00Z \
      --delete
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


DEFAULT_METRICS = ("cpatempwi_celsius", "cpatempwo_celsius")
DEFAULT_INSTANCE = "fridge-dodo"
DEFAULT_JOB = "sensor_data"
DEFAULT_LOW = -18.5
DEFAULT_HIGH = -17.0
DEFAULT_STEP = "15s"


@dataclass(frozen=True)
class BadWindow:
    metric: str
    start: datetime
    end: datetime
    samples: int
    min_value: float
    max_value: float

    @property
    def selector(self) -> str:
        return f'{self.metric}{{job="{DEFAULT_JOB}",instance="{DEFAULT_INSTANCE}"}}'

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find and optionally delete Dodo cooling-water temperature artifacts from Prometheus."
    )
    parser.add_argument(
        "--prometheus-url",
        default="http://localhost:9090",
        help="Prometheus base URL (default: http://localhost:9090)",
    )
    parser.add_argument(
        "--start",
        required=True,
        help="Range start, RFC3339 or unix seconds",
    )
    parser.add_argument(
        "--end",
        required=True,
        help="Range end, RFC3339 or unix seconds",
    )
    parser.add_argument(
        "--step",
        default=DEFAULT_STEP,
        help="Prometheus query_range step width (default: 15s)",
    )
    parser.add_argument(
        "--low",
        type=float,
        default=DEFAULT_LOW,
        help="Lower bound for suspicious values in C (default: -18.5)",
    )
    parser.add_argument(
        "--high",
        type=float,
        default=DEFAULT_HIGH,
        help="Upper bound for suspicious values in C (default: -17.0)",
    )
    parser.add_argument(
        "--instance",
        default=DEFAULT_INSTANCE,
        help="Instance label to scrub (default: fridge-dodo)",
    )
    parser.add_argument(
        "--job",
        default=DEFAULT_JOB,
        help="Job label to scrub (default: sensor_data)",
    )
    parser.add_argument(
        "--metric",
        action="append",
        dest="metrics",
        choices=DEFAULT_METRICS,
        help="Metric to inspect; repeat to limit the scan (default: both water-temp metrics)",
    )
    parser.add_argument(
        "--max-window-seconds",
        type=float,
        help="Only keep suspicious windows whose duration is at most this many seconds",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the detected windows through the Prometheus admin API",
    )
    parser.add_argument(
        "--clean-tombstones",
        action="store_true",
        help="Run clean_tombstones after delete",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Create a TSDB snapshot before delete",
    )
    return parser.parse_args()


def parse_time(value: str) -> datetime:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except ValueError:
        pass

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_step_seconds(value: str) -> float:
    units = {
        "ms": 0.001,
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 7 * 86400,
        "y": 365 * 86400,
    }
    for suffix in ("ms", "s", "m", "h", "d", "w", "y"):
        if value.endswith(suffix):
            number = float(value[: -len(suffix)])
            return number * units[suffix]
    return float(value)


def format_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def http_json(url: str, params: list[tuple[str, str]] | None = None, method: str = "GET") -> dict:
    full_url = url
    data = None
    headers: dict[str, str] = {}
    if params:
        encoded = urllib.parse.urlencode(params)
        if method == "GET":
            separator = "&" if "?" in url else "?"
            full_url = f"{url}{separator}{encoded}"
        else:
            data = encoded.encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(full_url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {"status": "success"}


def ensure_success(payload: dict, context: str) -> None:
    if payload.get("status") != "success":
        raise RuntimeError(f"{context} failed: {payload}")


def query_metric_range(
    prometheus_url: str,
    metric: str,
    job: str,
    instance: str,
    start: datetime,
    end: datetime,
    step: str,
) -> list[tuple[datetime, float]]:
    selector = f'{metric}{{job="{job}",instance="{instance}"}}'
    payload = http_json(
        f"{prometheus_url.rstrip('/')}/api/v1/query_range",
        params=[
            ("query", selector),
            ("start", format_time(start)),
            ("end", format_time(end)),
            ("step", step),
        ],
    )
    ensure_success(payload, f"query_range for {selector}")
    results = payload.get("data", {}).get("result", [])
    if not results:
        return []
    if len(results) != 1:
        raise RuntimeError(f"Expected one series for {selector}, got {len(results)}")
    values = []
    for raw_ts, raw_value in results[0].get("values", []):
        value = float(raw_value)
        if math.isfinite(value):
            values.append((datetime.fromtimestamp(float(raw_ts), tz=timezone.utc), value))
    return values


def find_bad_windows(
    metric: str,
    points: list[tuple[datetime, float]],
    low: float,
    high: float,
    step_seconds: float,
    job: str,
    instance: str,
) -> list[BadWindow]:
    if not points:
        return []

    allowed_gap = timedelta(seconds=max(step_seconds * 1.5, 1.0))
    windows: list[BadWindow] = []
    current: list[tuple[datetime, float]] = []
    previous_ts: datetime | None = None

    for timestamp, value in points:
        suspicious = low <= value <= high
        contiguous = previous_ts is not None and (timestamp - previous_ts) <= allowed_gap

        if suspicious and (not current or contiguous):
            current.append((timestamp, value))
        elif suspicious:
            windows.append(make_window(metric, current, step_seconds, job, instance))
            current = [(timestamp, value)]
        elif current:
            windows.append(make_window(metric, current, step_seconds, job, instance))
            current = []

        previous_ts = timestamp

    if current:
        windows.append(make_window(metric, current, step_seconds, job, instance))

    return windows


def make_window(
    metric: str,
    points: list[tuple[datetime, float]],
    step_seconds: float,
    job: str,
    instance: str,
) -> BadWindow:
    del job, instance
    values = [value for _, value in points]
    return BadWindow(
        metric=metric,
        start=points[0][0],
        end=points[-1][0] + timedelta(seconds=step_seconds),
        samples=len(points),
        min_value=min(values),
        max_value=max(values),
    )


def check_admin_api(prometheus_url: str) -> None:
    payload = http_json(f"{prometheus_url.rstrip('/')}/api/v1/status/flags")
    ensure_success(payload, "flags query")
    flags = payload.get("data", {})
    admin_enabled = str(flags.get("web.enable-admin-api", "false")).lower() == "true"
    if not admin_enabled:
        raise RuntimeError(
            "Prometheus admin API is disabled. Restart Prometheus with --web.enable-admin-api before using --delete."
        )


def snapshot_tsdb(prometheus_url: str) -> None:
    payload = http_json(
        f"{prometheus_url.rstrip('/')}/api/v1/admin/tsdb/snapshot",
        method="POST",
    )
    ensure_success(payload, "snapshot")
    name = payload.get("data", {}).get("name", "<unknown>")
    print(f"Snapshot created: {name}")


def delete_window(prometheus_url: str, window: BadWindow, job: str, instance: str) -> None:
    selector = f'{window.metric}{{job="{job}",instance="{instance}"}}'
    params = [
        ("match[]", selector),
        ("start", format_time(window.start)),
        ("end", format_time(window.end)),
    ]
    http_json(
        f"{prometheus_url.rstrip('/')}/api/v1/admin/tsdb/delete_series",
        params=params,
        method="POST",
    )


def clean_tombstones(prometheus_url: str) -> None:
    http_json(
        f"{prometheus_url.rstrip('/')}/api/v1/admin/tsdb/clean_tombstones",
        method="POST",
    )
    print("Cleaned tombstones.")


def print_windows(windows: list[BadWindow], job: str, instance: str) -> None:
    if not windows:
        print("No suspicious windows found.")
        return
    print(f"Found {len(windows)} suspicious window(s):")
    for window in windows:
        selector = f'{window.metric}{{job="{job}",instance="{instance}"}}'
        print(
            f"- {selector} | {format_time(window.start)} -> {format_time(window.end)} | "
            f"duration={window.duration_seconds:.0f}s | samples={window.samples} | "
            f"value_range=[{window.min_value:.3f}, {window.max_value:.3f}]"
        )


def main() -> int:
    args = parse_args()
    start = parse_time(args.start)
    end = parse_time(args.end)
    if end <= start:
        raise SystemExit("--end must be after --start")

    step_seconds = parse_step_seconds(args.step)
    metrics = tuple(args.metrics or DEFAULT_METRICS)
    all_windows: list[BadWindow] = []

    for metric in metrics:
        points = query_metric_range(
            args.prometheus_url,
            metric,
            args.job,
            args.instance,
            start,
            end,
            args.step,
        )
        windows = find_bad_windows(
            metric,
            points,
            args.low,
            args.high,
            step_seconds,
            args.job,
            args.instance,
        )
        all_windows.extend(windows)

    if args.max_window_seconds is not None:
        all_windows = [
            window for window in all_windows if window.duration_seconds <= args.max_window_seconds
        ]

    all_windows.sort(key=lambda window: (window.metric, window.start))
    print_windows(all_windows, args.job, args.instance)

    if not args.delete:
        print("Dry run only. Re-run with --delete to remove these windows.")
        return 0

    if not all_windows:
        print("Nothing to delete.")
        return 0

    check_admin_api(args.prometheus_url)
    if args.snapshot:
        snapshot_tsdb(args.prometheus_url)

    for window in all_windows:
        delete_window(args.prometheus_url, window, args.job, args.instance)
        print(
            f"Deleted {window.metric} window {format_time(window.start)} -> {format_time(window.end)}"
        )

    if args.clean_tombstones:
        clean_tombstones(args.prometheus_url)
    else:
        print("Skipped clean_tombstones. Re-run with --clean-tombstones if you want to compact deletions now.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP error {exc.code}: {details}", file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as exc:
        print(f"Connection error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)