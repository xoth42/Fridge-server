#!/usr/bin/env python3
"""
Simple Slack slash-command tester for `/alerts`.

Usage examples:
  # basic: uses SLACK_SIGNING_SECRET from env
  export SLACK_SIGNING_SECRET="$(cat .env | sed -n 's/^SLACK_SIGNING_SECRET=\(.*\)$/\1/p')"
  python3 testslack/test_alerts.py --url http://localhost:8000/api/slack/commands

  # start a local receiver so the alert-api can POST the table back
  python3 testslack/test_alerts.py --url http://localhost:8000/api/slack/commands --listen 9999

Notes:
- The script signs the request with the Slack signing secret (HMAC-SHA256).
- If you use Docker, ensure the alert-api container can reach the host's
  callback URL (use host.docker.internal or an accessible IP).
"""

import argparse
import os
import time
import hmac
import hashlib
import urllib.parse
import urllib.request
import urllib.error
import json
import threading
import http.server
import socketserver
import sys


def compute_signature(secret: str, timestamp: str, body_bytes: bytes) -> str:
    basestring = f"v0:{timestamp}:".encode("utf-8") + body_bytes
    mac = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256)
    return "v0=" + mac.hexdigest()


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    last_body = None
    event = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        print("\n--- Received callback POST ---")
        print(self.command, self.path)
        print("Headers:")
        for k, v in self.headers.items():
            print(f"  {k}: {v}")
        try:
            parsed = json.loads(body.decode())
            print("JSON payload:")
            print(json.dumps(parsed, indent=2))
        except Exception:
            print("Body (raw):")
            print(body.decode(errors="replace"))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
        CallbackHandler.last_body = body
        if CallbackHandler.event:
            CallbackHandler.event.set()

    def log_message(self, format, *args):
        # suppress default logging
        return


def start_listener(port: int):
    CallbackHandler.last_body = None
    CallbackHandler.event = threading.Event()
    server = socketserver.TCPServer(("0.0.0.0", port), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, CallbackHandler


def build_form_payload(response_url: str, text: str = "") -> (bytes, dict):
    form = {
        "team_id": "T123",
        "team_domain": "local",
        "channel_id": "C123",
        "channel_name": "test",
        "user_id": "U123",
        "user_name": "tester",
        "command": "/alerts",
        "text": text,
        "response_url": response_url,
        "trigger_id": "dummy",
    }
    body_str = urllib.parse.urlencode(form)
    body_bytes = body_str.encode("utf-8")
    return body_bytes, form


def post_slash(url: str, secret: str, body_bytes: bytes):
    ts = str(int(time.time()))
    sig = compute_signature(secret, ts, body_bytes)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig,
    }
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_body = resp.read()
            status = resp.getcode()
            ctype = resp.headers.get('Content-Type','')
            print(f"\n--- Immediate response (HTTP {status}) ---")
            if 'application/json' in ctype:
                try:
                    print(json.dumps(json.loads(resp_body.decode()), indent=2))
                except Exception:
                    print(resp_body.decode(errors='replace'))
            else:
                print(resp_body.decode(errors='replace'))
            return status, resp_body
    except urllib.error.HTTPError as e:
        print(f"HTTPError: {e.code}")
        try:
            print(e.read().decode())
        except Exception:
            pass
        return getattr(e, 'code', None), None
    except Exception as e:
        print("Request error:", e)
        return None, None


def main():
    p = argparse.ArgumentParser(description="Test /alerts Slack slash command")
    p.add_argument("--url", default="http://localhost:8000/api/slack/commands", help="Slash command URL")
    p.add_argument("--secret", help="Slack signing secret (or use SLACK_SIGNING_SECRET env)")
    p.add_argument("--listen", type=int, help="Start local HTTP listener to capture response_url POST")
    p.add_argument("--response-url", help="Explicit response_url to send (overrides --listen)")
    p.add_argument("--text", default="", help="Command text")
    args = p.parse_args()

    secret = args.secret or os.getenv("SLACK_SIGNING_SECRET")
    if not secret:
        print("Error: SLACK_SIGNING_SECRET not provided. Set --secret or SLACK_SIGNING_SECRET env var.")
        sys.exit(1)

    server = None
    if args.listen:
        srv_port = args.listen
        try:
            server, handler = start_listener(srv_port)
        except Exception as e:
            print(f"Failed to start listener on port {srv_port}: {e}")
            sys.exit(1)
        # Note: containers must be able to reach this host. Use host.docker.internal or host IP if needed.
        response_url = args.response_url or f"http://host.docker.internal:{srv_port}/slack_resp"
        print(f"Started local listener on port {srv_port}; using response_url={response_url}")
    else:
        response_url = args.response_url or "http://example.invalid/resp"

    body_bytes, form = build_form_payload(response_url, args.text)
    print("Posting signed slash command to:", args.url)
    status, resp_body = post_slash(args.url, secret, body_bytes)

    if server:
        print("Waiting up to 10s for the background response_url POST...")
        ok = handler.event.wait(10)
        if ok:
            print("Callback received (printed above).")
        else:
            print("No callback received within 10s. If using Docker, ensure the container can reach the host address used in response_url.")
        server.shutdown()


if __name__ == '__main__':
    main()
