"""Login cookie: read at runtime from 1Password through the fleet's cached reader.

The cookie value lives only in process memory. It is never printed, logged,
written to disk by this package, or passed on a command line.
"""

from __future__ import annotations

import os
import shutil
import subprocess

# AI vault, item "Spotify web session sp_dc", field "credential". Referenced by id so the
# title's characters never matter.
DEFAULT_OP_REF = "op://p5fyqekdzsdp7rmdk3n4kjecse/kswa6vl3peh44le5oitsvisj4y/credential"
READER_CANDIDATES = (
    os.path.expanduser("~/.hermes/scripts/op-cached-read"),
    os.path.expanduser("~/.local/bin/op-cached-read"),
)


class CookieError(RuntimeError):
    """The cookie could not be obtained. The message never contains the value."""


def _reader() -> str:
    override = os.environ.get("QS_OP_READER")
    if override:
        return override
    for path in READER_CANDIDATES:
        if os.access(path, os.X_OK):
            return path
    found = shutil.which("op-cached-read")
    if found:
        return found
    raise CookieError("op-cached-read not found; cannot read the Spotify cookie")


def read_sp_dc(*, fresh: bool = False) -> str:
    """Return the sp_dc value. The 1Password reference (not the secret) is the only argv."""
    ref = os.environ.get("QS_OP_REF", DEFAULT_OP_REF)
    argv = [_reader()]
    if fresh:
        argv.append("--fresh")
    argv.append(ref)
    proc = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    value = proc.stdout.strip()
    if proc.returncode != 0 or not value:
        # stderr of the reader never carries the value; keep only its first line.
        hint = (proc.stderr or "").strip().splitlines()[:1]
        raise CookieError(f"1Password read failed (rc {proc.returncode}): {' '.join(hint)}")
    return value


def cookie_header(sp_dc: str) -> str:
    return "sp_dc=" + sp_dc


def scrub(text: str, secret: str | None) -> str:
    """Remove a secret from text before it is shown anywhere."""
    if secret and secret in text:
        return text.replace(secret, "<redacted>")
    return text
