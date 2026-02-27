#!/usr/bin/env python3
"""Push synthetic fridge metrics to Pushgateway."""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request


def build_payload() -> str:
    # Values are deterministic so alert-pipeline tests are reproducible.
    # sensor-1: 38.0  – below the FridgeSyntheticMetricHigh threshold (42)
    # sensor-2: 45.0  – above threshold, expected to trigger alert
    # sensor-3: 50.0  – above threshold, expected to trigger alert
    samples = [38.0, 45.0, 50.0]
    lines = [
        "# TYPE fridgetestmetric gauge",
    ]
    for idx, value in enumerate(samples, start=1):
        lines.append(
            f'fridgetestmetric{{fridge="test-fridge-1",sensor="mxc-{idx}"}} {value:.2f}'
        )
    return "\n".join(lines) + "\n"


def push_metrics(base_url: str) -> None:
    payload = build_payload().encode("utf-8")
    endpoint = base_url.rstrip("/") + "/metrics/job/test-job/instance/test-instance"
    request = urllib.request.Request(endpoint, data=payload, method="PUT")
    request.add_header("Content-Type", "text/plain; version=0.0.4")
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status not in (200, 202):
            raise RuntimeError(f"Unexpected status code from Pushgateway: {response.status}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Push synthetic fridge metrics to Pushgateway")
    parser.add_argument(
        "--pushgateway-url",
        default="http://localhost:9091",
        help="Pushgateway base URL (default: http://localhost:9091)",
    )
    args = parser.parse_args()

    try:
        push_metrics(args.pushgateway_url)
        print(f"Pushed synthetic metrics to {args.pushgateway_url}")
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"Failed to push metrics: {exc}", file=sys.stderr)
        sys.exit(1)
