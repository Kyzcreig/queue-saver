"""queue-saver park|restore|list|probe"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Callable

from . import ops
from .session import CookieError, cookie_header, read_sp_dc, reseed_from_container, scrub
from .spotify import SpotifyBackend, assert_pinned

BackendFactory = Callable[[str], Any]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="queue-saver", description=__doc__)
    sub = p.add_subparsers(dest="verb", required=True)
    pr = sub.add_parser("probe", help="show device, context and upcoming counts by provider")
    pr.add_argument("--seed", type=int, default=0, help="queue up to N tracks, count, remove exactly those")
    pk = sub.add_parser("park", help="snapshot, copy to a private playlist, remove queued songs")
    pk.add_argument("--queued-only", action="store_true", help="playlist holds only the queued songs")
    pk.add_argument("--name-prefix", default="Queue Saver", help="playlist name prefix")
    rs = sub.add_parser("restore", help="put a parked state back")
    rs.add_argument("snapshot", nargs="?", help="snapshot name or path (default: latest)")
    rs.add_argument("--device", help="device NAME to play on when none is active")
    sub.add_parser("list", help="show parked states")
    return p


async def _run(args: argparse.Namespace, out: Callable[[str], None], factory: BackendFactory,
               version: str, secret: list[str] | None = None) -> int:
    secret = secret if secret is not None else []

    def cookie(fresh: bool = False) -> str:
        value = read_sp_dc(fresh=fresh)
        secret.append(value)  # only so output can be scrubbed of it
        return cookie_header(value)

    async def login() -> Any:
        return factory(cookie())

    backend = None
    try:
        backend = await login()
        async with backend as sp:
            return await _dispatch(args, sp, out, version)
    except Exception as e:
        cause = e.__cause__ if isinstance(e, ops.StageError) else e
        if not _is_login_failure(cause) or _mutated(backend):
            raise
        out("login rejected; reseeding the cookie from the browser container and retrying once")
        if not reseed_from_container():
            out("reseed did not update the cookie; retrying with a fresh 1Password read")
        backend = factory(cookie(fresh=True))
        async with backend as sp:
            return await _dispatch(args, sp, out, version)


# Backend calls that change nothing. Any other logged call may have reached Spotify, so a login
# failure after it is ambiguous and the command is not replayed.
READ_ONLY_CALLS = {"refresh", "playlist_uris"}


def _mutated(backend: Any) -> bool:
    return any(name not in READ_ONLY_CALLS for name in getattr(backend, "log", ()))


def _is_login_failure(e: BaseException | None) -> bool:
    """spotapi 2.1.0's wheel has no LoginExpired (its docs at 7ec4932 do); use the HTTP status."""
    from spotapi.types.exceptions import HTTPError

    if type(e).__name__ == "LoginExpired":
        return True
    return isinstance(e, HTTPError) and getattr(e, "status_code", getattr(e, "status", None)) == 401


async def _dispatch(args: argparse.Namespace, sp: Any, out: Callable[[str], None], version: str) -> int:
    if args.verb == "probe":
        return await ops.probe(sp, out, seed=args.seed)
    if args.verb == "park":
        return await ops.park(sp, out, version=version, queued_only=args.queued_only,
                              name_prefix=args.name_prefix)
    if args.verb == "restore":
        return await ops.restore(sp, out, name=args.snapshot, device=args.device)
    raise SystemExit(f"unknown verb {args.verb}")


def main(argv: list[str] | None = None, *, factory: BackendFactory = SpotifyBackend,
         out: Callable[[str], None] | None = None) -> int:
    args = build_parser().parse_args(argv)
    printer = out or (lambda s: print(s, flush=True))
    if args.verb == "list":
        return ops.list_parked(printer)
    try:
        version = assert_pinned()
    except RuntimeError as e:
        printer(f"error: {e}")
        return 4

    secret: list[str] = []

    def safe(s: str) -> None:
        for v in secret:
            s = scrub(s, v)
        printer(s)

    try:
        return asyncio.run(_run(args, safe, factory, version, secret))
    except ops.StageError as e:
        safe(f"error: {e}")
        return 2
    except CookieError as e:
        safe(f"error: cookie: {e}")
        return 2
    except Exception as e:  # any private-API surprise fails loud, naming the type
        safe(f"error: {args.verb}: {type(e).__name__}: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
