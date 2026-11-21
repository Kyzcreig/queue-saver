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
    try:
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
    finally:  # closing the socket only detaches; close the tab this script opened
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{target['id']}", timeout=60).read()
        except OSError:
            print("reseed: could not close the tab it opened")
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
    # A piped template is the whole item (op item edit --help: get the item JSON, edit it, pipe it
    # back), so start from the full item and change only the credential field's value.
    got = subprocess.run(["op", "item", "get", ITEM, "--vault", VAULT, "--format", "json"],
                         capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    if got.returncode != 0:
        print(f"reseed: op item get failed rc {got.returncode}")
        return 1
    template = json.loads(got.stdout)
    fields = [f for f in template.get("fields", []) if f.get("id") == "credential"]
    if len(fields) != 1:
        print("reseed: item has no single credential field; not editing")
        return 1
    fields[0]["value"] = value
    proc = subprocess.run(["op", "item", "edit", ITEM, "--vault", VAULT, "--format", "json"],
                          input=json.dumps(template), capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        print(f"reseed: op item edit failed rc {proc.returncode}")
        return 1
    print(f"reseed: updated item {ITEM} (value length {len(value)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
