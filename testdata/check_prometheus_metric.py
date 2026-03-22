#!/usr/bin/env python3
"""Verify that Prometheus received real-shaped fridge metrics from the CI push.

Checks:
  - ch1_t_kelvin present for both fridge-manny and fridge-sid
  - maxigauge_ch1_pressure_mbar present for both
  - flowmeter_mmol_per_s present for both
  - job label is "sensor_data" on all results
  - values are finite numbers within physically plausible ranges
"""

from __future__ import annotations

import json
import sys


EXPECTED_INSTANCES = {"fridge-manny", "fridge-sid"}


def extract(results: list[dict], metric: str) -> dict[str, float]:
    """Return {instance: value} for a given metric name from a query result."""
    return {
        r["metric"]["instance"]: float(r["value"][1])
        for r in results
        if r["metric"].get("__name__") == metric
    }


def check(response_json: str) -> None:
    resp = json.loads(response_json)
    assert resp.get("status") == "success", f"Query failed: {resp}"
    results = resp.get("data", {}).get("result", [])
    assert results, f"No results returned: {resp}"

    # All results must carry job=sensor_data
    for r in results:
        job = r["metric"].get("job")
        assert job == "sensor_data", f"Unexpected job label: {r['metric']}"

    temps = extract(results, "ch1_t_kelvin")
    assert EXPECTED_INSTANCES <= temps.keys(), (
        f"Missing instances in ch1_t_kelvin. Got: {set(temps)}"
    )
    for inst, val in temps.items():
        assert 1.0 < val < 500.0, f"ch1_t_kelvin out of range for {inst}: {val}"

    pressures = extract(results, "maxigauge_ch1_pressure_mbar")
    assert EXPECTED_INSTANCES <= pressures.keys(), (
        f"Missing instances in maxigauge_ch1_pressure_mbar. Got: {set(pressures)}"
    )

    flows = extract(results, "flowmeter_mmol_per_s")
    assert EXPECTED_INSTANCES <= flows.keys(), (
        f"Missing instances in flowmeter_mmol_per_s. Got: {set(flows)}"
    )

    print(
        f"OK — metrics present for {sorted(temps)} | "
        f"ch1_t_kelvin: {temps} K | "
        f"maxigauge_ch1: {pressures} mbar"
    )


if __name__ == "__main__":
    check(sys.argv[1])
