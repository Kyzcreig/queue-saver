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
  nothing (exit 1, snapshot and playlist kept) unless the visible queue is shorter than the read
  cap. Measured cap: a state read shows at most 80 upcoming entries, queued songs first, so park
  clears up to 79 queued songs and refuses at 80 or more.
- That `set_queue` sends back every kept entry exactly as Spotify sent it (provider, uid, metadata)
  and the history (`prev_tracks`) unchanged; the only difference is the removed queued songs. Songs
  queued from a phone (`provider: "context"` + `metadata.is_queued: "true"`) are counted as queued
  on a copy of the state, never re-labelled in what is written back.
- After the clear, `park` reads the player for 5 s. If it was playing before and reads paused
  after, it prints `!!! PLAYBACK STOPPED ...` and exits 1. It never presses play itself.
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

## Live self-test (no owner phone)

Test on a device the fleet controls, never on Ace's phone or a house speaker/TV.

1. `uv run queue-saver probe`. If another device (e.g. `iPhone`) is active and `playing=True`, stop:
   do not transfer, pause or queue on it; re-check every 2 minutes.
2. Make sure nothing in the house can sound: `osascript -e 'get volume settings'` must say
   `output muted:true` (or output volume 0).
3. Start a test device. The container web player (`id-spotify-kyzcreig`) and headless Chrome have no
   Widevine, so they never appear as a Connect device. Use the Spotify desktop app on the Mac Studio
   (device name `Mac Studio M3U`, signed in as kyzcreig): `open -g -a Spotify`. If `/Applications/Spotify.app`
   fails code signing (`log show ... OS_REASON_CODESIGNING`), run a fresh copy from scratch instead:
   `curl -o Spotify.dmg https://download.scdn.co/SpotifyARM64.dmg`, mount it, `ditto` the app into the
   scratch dir and `open -g -a <scratch>/Spotify.app`.
4. Run `probe` again (rule 1), then play any playlist on that device at volume 0:
   `uv run python scripts/selftest_play.py play "Mac Studio M3U" spotify:playlist:<id>`
   (it refuses when another device is playing, and refuses speaker/TV/phone names).
5. `uv run queue-saver probe --seed 30` must end `seeded 30, visible >=30 (30), restored: yes`.
6. `uv run queue-saver probe --seed 3 --keep` leaves 3 queued songs. `uv run queue-saver park` must
   print the playlist link and `queued visible now: 0`. Then `uv run queue-saver restore` and
   `uv run queue-saver probe`: `queue=3`, the same songs in the saved order.
7. `uv run python scripts/selftest_play.py pause "Mac Studio M3U"`, quit the app, and delete the
   `Queue Saver <date>` playlists the run created.

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
