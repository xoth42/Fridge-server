#!/usr/bin/env python3
"""Verify that a Prometheus query API response contains the expected metric."""

from __future__ import annotations

import json
import sys


def check(response_json: str) -> None:
    resp = json.loads(response_json)
    assert resp.get("status") == "success", resp
    results = resp.get("data", {}).get("result", [])
    assert results, f"Expected non-empty result, got: {resp}"
    print("Prometheus query returned synthetic metric")


if __name__ == "__main__":
    check(sys.argv[1])
