#!/usr/bin/env python3
"""Reseed the 1Password sp_dc item from the fleet browser identity (run INSIDE a read lease).

    browserctl with id-spotify-kyzcreig --class read --ttl 300 -- \
        /usr/bin/python3 ~/.hermes/scripts/browsers/lease-cdp-forward.py -- \
        <python with websockets> scripts/reseed_sp_dc.py

Reads the sp_dc cookie for open.spotify.com over the lease's CDP port and pipes an item template
to `op item edit` on stdin. Never prints, logs or passes the value on a command line. Exit 2 when
the container holds no sp_dc (not logged in): this script never attempts a login.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

from websockets.sync.client import connect

ITEM = os.environ.get("QS_OP_ITEM", "kswa6vl3peh44le5oitsvisj4y")
VAULT = os.environ.get("QS_OP_VAULT", "p5fyqekdzsdp7rmdk3n4kjecse")
TOKEN_FILE = os.path.expanduser(os.environ.get("QS_OP_TOKEN_FILE", "~/.hermes/.op-service-token"))
ENV_NAME = "OP_SERVICE_ACCOUNT" + "_TOKEN"


def read_cookie(port: str) -> str:
    def http(path: str, method: str = "GET") -> dict:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
        return json.loads(urllib.request.urlopen(req, timeout=60).read() or b"{}")

    target = http("/json/new?https://open.spotify.com/", "PUT")
    ws = connect(target["webSocketDebuggerUrl"].replace("localhost", "127.0.0.1"), open_timeout=30)
    try:
        time.sleep(8)
        ws.send(json.dumps({"id": 1, "method": "Network.getCookies",
                            "params": {"urls": ["https://open.spotify.com/"]}}))
        while True:
            reply = json.loads(ws.recv(timeout=60))
            if reply.get("id") == 1:
                break
    finally:
        ws.close()
    cookies = reply.get("result", {}).get("cookies", [])
    return next((c["value"] for c in cookies if c.get("name") == "sp_dc"), "")


def main() -> int:
    value = read_cookie(os.environ["CDP_PORT"])
    if not value:
        print("reseed: no sp_dc in the container (not logged in); not attempting a login")
        return 2
    env = dict(os.environ)
    with open(TOKEN_FILE) as fh:
        env[ENV_NAME] = fh.read().strip()
    template = {"fields": [{"id": "credential", "type": "CONCEALED", "label": "credential", "value": value}]}
    proc = subprocess.run(["op", "item", "edit", ITEM, "--vault", VAULT, "--format", "json"],
                          input=json.dumps(template), capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        print(f"reseed: op item edit failed rc {proc.returncode}")
        return 1
    print(f"reseed: updated item {ITEM} (value length {len(value)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
