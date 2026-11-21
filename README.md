# Queue Saver v2

Park your Spotify listening, host a party on other music, and pick your own listening back up
afterwards, without skipping a single song.

A small command line tool on the Mac Studio, built on [`spotapi`](https://github.com/Aran404/SpotAPI)
2.1.0 (Spotify web player private endpoints, pinned dependency, never vendored). Spec:
[`docs/SPEC-queue-saver-v2.md`](docs/SPEC-queue-saver-v2.md).

## Verbs

```
queue-saver probe [--seed N]          # device, context, upcoming counts by provider (queue/context)
queue-saver park [--queued-only]      # snapshot -> private playlist -> read back -> remove queued songs
queue-saver restore [SNAPSHOT] [--device NAME]
queue-saver list                      # parked states
```

- `park` never touches playback (no next, previous, pause, play or seek). It writes the snapshot
  file first, creates a private playlist (current song + everything upcoming, in play order;
  `--queued-only` keeps only songs you queued), reads the playlist back, and only then removes the
  queued songs it saved, in one `set_queue`. `set_queue` replaces the whole queue, so park removes
  nothing (exit 1, snapshot and playlist kept) unless the visible queue is shorter than a known read
  cap. The live backend's cap is unknown, so on spotapi 2.1.0 park saves but does not clear.
- `restore` plays the saved context at the saved song and position on the ACTIVE device, then puts
  the queued songs back in order, first; songs queued since the park stay, after them. With no active device it lists devices and exits 3 unless
  `--device NAME` is given. If the context no longer exists it restores the song and the queued
  songs and says so.
- Snapshots: `~/.hermes/var/queue-saver/snapshots/<timestamp>.json` (override with `QS_STATE_DIR`).

Exit codes: 0 ok, 1 check failed (e.g. read-back mismatch), 2 a named stage failed (any spotapi
error or a state missing a field), 3 no active device, 4 spotapi version is not the pin.

## Login

The tool needs the `sp_dc` cookie of the Spotify web session. It reads it at run time through the
fleet's cached 1Password reader (`op-cached-read`), from the AI vault item
"Spotify web session sp_dc" (`QS_OP_REF` overrides the reference). The value lives only in process
memory: never in the repo, argv, logs, stdout or snapshot files. On a 401 it re-reads 1Password once.
To reseed after the cookie expires, read `sp_dc` for open.spotify.com from the fleet browser identity
`id-spotify-kyzcreig` over a read lease and update the item.

## Setup and tests

```
uv sync                # Python 3.12 venv, spotapi==2.1.0 from its wheel (uv.lock committed)
uv run pytest -q       # offline, against recorded-shape state fixtures
uv run queue-saver probe
```

Using the private API is against Spotify's terms; the tool makes a handful of calls per run, only
when asked. When Spotify changes its web player, bump the pin, rerun `probe` and the tests.

v1 (the single `index.html` on the official Web API, 20-item window) was removed in v2; `git revert`
of that commit brings it back.
