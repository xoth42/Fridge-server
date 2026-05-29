#!/usr/bin/env python3
"""Verify that the Grafana Prometheus datasource is provisioned."""

from __future__ import annotations

import json
import sys


def check(response_json: str) -> None:
    obj = json.loads(response_json)
    assert obj.get("name") == "Prometheus", obj
    print("Grafana datasource exists")


if __name__ == "__main__":
    check(sys.argv[1])
