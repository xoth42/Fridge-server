#!/usr/bin/env python3
"""Verify that the Fridge Test dashboard is provisioned in Grafana."""

from __future__ import annotations

import json
import sys


def check(response_json: str) -> None:
    arr = json.loads(response_json)
    assert any(d.get("title") == "Fridge Test" for d in arr), arr
    print("Grafana dashboard is provisioned")


if __name__ == "__main__":
    check(sys.argv[1])
