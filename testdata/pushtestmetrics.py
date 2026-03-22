#!/usr/bin/env python3
"""Push synthetic fridge metrics to Pushgateway.

Metrics match the real label structure produced by push_metrics.py on the
fridge computers (job=sensor_data, instance=fridge-<name>). Values are
physically plausible for a cold Bluefors fridge.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request


# Metrics to push per fridge. Covers the key subsystems (temperatures,
# pressures, flow) that dashboards and alert rules will query.
FRIDGE_METRICS: dict[str, dict[str, float]] = {
    "fridge-manny": {
        "ch1_t_kelvin": 42.0,       # 50K flange
        "ch2_t_kelvin": 3.1,        # 4K flange
        "ch9_t_kelvin": 0.015,      # mixing chamber (15 mK)
        "maxigauge_ch1_pressure_mbar": 2.5e-6,  # high vacuum
        "maxigauge_ch2_pressure_mbar": 0.044,
        "flowmeter_mmol_per_s": 0.47,
        "heater_0_watts": 0.0,
        "heater_1_watts": 0.01,
        "last_push_timestamp_seconds": 0.0,  # overwritten at push time; 0 is fine for CI
    },
    "fridge-sid": {
        "ch1_t_kelvin": 45.0,
        "ch2_t_kelvin": 3.4,
        "ch9_t_kelvin": 0.020,
        "maxigauge_ch1_pressure_mbar": 3.1e-6,
        "maxigauge_ch2_pressure_mbar": 0.051,
        "flowmeter_mmol_per_s": 0.50,
        "heater_0_watts": 0.0,
        "heater_1_watts": 0.0,
        "last_push_timestamp_seconds": 0.0,
    },
}


def build_payload(metrics: dict[str, float]) -> str:
    lines: list[str] = []
    for name, value in metrics.items():
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"


def push_fridge(base_url: str, instance: str, metrics: dict[str, float]) -> None:
    payload = build_payload(metrics).encode("utf-8")
    endpoint = (
        base_url.rstrip("/")
        + f"/metrics/job/sensor_data/instance/{instance}"
    )
    request = urllib.request.Request(endpoint, data=payload, method="PUT")
    request.add_header("Content-Type", "text/plain; version=0.0.4")
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status not in (200, 202):
            raise RuntimeError(
                f"Unexpected status {response.status} pushing to {endpoint}"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Push synthetic fridge metrics to Pushgateway")
    parser.add_argument(
        "--pushgateway-url",
        default="http://localhost:9091",
        help="Pushgateway base URL (default: http://localhost:9091)",
    )
    args = parser.parse_args()

    failed = False
    for instance, metrics in FRIDGE_METRICS.items():
        try:
            push_fridge(args.pushgateway_url, instance, metrics)
            print(f"Pushed {len(metrics)} metrics for {instance}")
        except (urllib.error.URLError, RuntimeError) as exc:
            print(f"Failed to push metrics for {instance}: {exc}", file=sys.stderr)
            failed = True

    sys.exit(1 if failed else 0)
