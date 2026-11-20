# Queue Saver v2: park and restore the Spotify queue without skipping

**Version:** v1
**Date:** 2026-11-20
**Owner:** Apollo
**Status:** DRAFT, build dispatched; Phase 0 probe gates Phases 2 and 3

## 1. Summary and goal

Ace wants to put his current listening aside, host a party on different music, and pick his own
listening back up afterwards. Queue Saver v1 (the single `index.html`, March 2026) saves the queue to
a playlist through Spotify's official Web API. That API shows only the next 20 tracks, so v1 skips
through the music to read deeper, and it cannot tell songs Ace queued from the playlist's own
upcoming songs.

v2 is a small Python command line tool on the Mac Studio built on `spotapi` 2.1.0, which speaks the
Spotify web player's private endpoints. The smallest change that meets the goal: three verbs.

- `park`: record the whole playback state, copy it to a private playlist, then remove the queued
  songs. Playback is not touched.
- `restore`: put a parked state back (same playlist or album, same song, same position, same queued
  songs in order).
- `list`: show parked states.

Measured starting point: the official queue endpoint returns 20 items; `spotapi`'s player state
(`PlayerState.next_tracks`, read from source at commit `7ec4932`) carries the upcoming list with each
track tagged `provider="queue"` or `"context"`. How deep that list goes on Ace's account is not
measured yet. Phase 0 measures it.

## 2. Non-goals

- No web page, phone button or voice command in v2 (roadmap, section 9).
- No change to Clanker, Home Assistant, Music Assistant or `cast.sh`.
- No copying of `spotapi` source into this repo. It is a pinned dependency.
- No always-on process. The tool runs when asked and exits.

## 3. Invariants

- **Never skip.** The tool never calls next, previous, pause, play or seek during `park`.
  - *Proof:* test asserts the command log of a `park` run contains only reads, playlist writes and one `set_queue`; live check that track URI and playing flag are equal before and after, and position advanced by no more than elapsed time plus 3 s.
- **Never lose a queue.** `park` removes queued songs only after the snapshot file is on disk and the new playlist has been read back with the expected item count.
  - *Proof:* fault-injection test: playlist read-back returns a short count, then the queue is unchanged and the exit code is non-zero.
- **The login cookie stays out of the repo, the logs, argv and stdout.**
  - *Proof:* test greps captured output and the snapshot files for the cookie value of a fake session; `git grep sp_dc` finds only variable names.
- **A private API change fails loud.** Any `SpotAPIError`, or a state that lacks a field the tool needs, exits non-zero naming the stage. No partial clear.
  - *Proof:* test feeds a state fixture with `next_tracks` missing.
- **Restore never starts sound on a device Ace did not pick.** It targets the currently active device; with none active it prints the device list and exits non-zero unless `--device NAME` is given.
  - *Proof:* test with a no-active-device fixture.

## 4. Resolved decisions

- RULED 2026-11-20 (Ace): rebuild on SpotAPI. Scope is this tool only.
- DECIDED 2026-11-20 (Apollo): command line tool, driven by Apollo in chat ("park my queue"). Reason: Ace talks to Apollo already; a page is a second surface to keep alive.
- DECIDED 2026-11-20 (Apollo): the playlist holds the current song plus everything upcoming, in play order (the superset, and what v1 did). `--queued-only` narrows it. The snapshot file records which songs were queued and which came from the context, so restore is exact either way.
- DECIDED 2026-11-20 (Apollo): the repo moves to `ANG-Ventures/queue-saver` (private) before v2 lands on the default branch. Measured: no fleet GitHub lane can push to or open a PR on `Kyzcreig/queue-saver`, and the trunk-push guard refuses a direct push. Until the move, work lives on branches of the current repo. A private repo that is never distributed needs no licence change; `LICENSE` is dropped from Phase 1.
- DECIDED 2026-11-20 (Apollo): v1 `index.html` is deleted in Phase 1. Rebuild check: a rebuild today would keep the dated private playlist and the link in the output; the skip loop, the browser OAuth page and the track-count box exist only because of the official API's 20-item window.
- scouted: `spotapi` 2.1.0, adopt as a pinned dependency. scouted: official Web API, skip (20-item window, measured in v1). scouted: `spotipy`, skip (same API).

## 5. Design

```
queue-saver park|restore|list|probe
      |
      +-- session.py   cookie from 1Password (AI vault, item "Spotify web session sp_dc");
      |                on a login failure, reseed from the fleet browser container
      |                id-spotify-kyzcreig and retry once
      +-- state.py     read PlayerState -> Snapshot (plain dataclass, JSON)
      +-- park.py      snapshot file -> playlist -> read back -> clear queued
      +-- restore.py   play context at saved song -> seek -> set queued songs -> read back
```

Snapshot JSON (one file per park, under the fleet state dir `var/queue-saver/snapshots/`, named by
timestamp): `taken_at`, `device_name`, `context_uri`, `track {uri, uid, name, artist}`,
`position_ms`, `shuffle`, `repeat`, `upcoming [{uri, uid, provider, name, artist}]`,
`playlist_url`, `spotapi_version`.

Runtime: a dedicated `uv` venv on Python 3.12, `spotapi==2.1.0` installed from the wheel, lockfile
committed. The tool asserts the installed version equals the pin at start.

## 6. Phases

- **Phase 0, probe (gates 2 and 3).** `queue-saver probe` prints: active device, context, count of upcoming tracks by provider. `probe --seed N` adds tracks until N are queued, reads the count, then removes exactly the tracks it added (by occurrence uid) and reads back that the queue equals the starting queue.
  - *E2E check:* with a session active, `probe --seed 25` reports 25 or more queued tracks visible. If it reports fewer, STOP: write the measured ceiling into this spec and hand back. The design changes if the private state is capped too.
  - *Negative:* no active device prints the device list and exits 3 without changing anything.
  - *Verify with:* `queue-saver probe --seed 25` ends `seeded 25, visible >=25, restored: yes`.
  - If no Spotify session is active when the worker reaches this phase, the worker does not start one. It finishes Phases 1 to 3 against recorded fixtures and hands back with the live checks listed as not run.
- **Phase 1, subtract.** Delete `index.html`; rewrite `README.md` for v2.
  - *Verify with:* `git ls-files` shows no `index.html`.
- **Phase 2, park and list.**
  - *Unit check:* snapshot round-trips through JSON; playlist item order equals current song then `upcoming` order; `--queued-only` keeps only `provider == "queue"`.
  - *E2E check:* on the live account with 3 seeded queued songs: `park` prints a playlist link; the playlist read back holds the expected count; queued songs visible afterwards: 0; current song unchanged.
  - *Negative:* the two fault-injection tests from section 3.
  - *Verify with:* `uv run pytest -q` green, then `queue-saver park` on the seeded session.
- **Phase 3, restore.**
  - *E2E check:* after the Phase 2 park, play something else, run `restore`: current song URI equals the snapshot's, position within 5 s of the saved position, queued songs equal the saved queued songs in order.
  - *Negative:* no active device and no `--device` exits non-zero with the device list; a snapshot whose context no longer exists restores the queued songs and says so.
  - *Verify with:* `queue-saver restore` then `queue-saver probe`.
- **Phase 4, hand to Apollo.** A short skill so "park my queue" and "restore my queue" in chat run the tool. skip: evals (no model in the tool). skip: review pipeline (single-user tool, every write is reversible by `restore` or by deleting a playlist).

## 7. Security and operations

- The cookie is Ace's main Spotify login. It lives in 1Password and in process memory, nowhere else.
- Using the private API is against Spotify's terms. The tool makes a handful of calls per run, only when asked.
- `spotapi` breaks when Spotify changes its web player (about 20 such issues on its tracker since March 2025). Remedy: bump the pin, rerun `probe` and the tests. The tool fails closed in the meantime.
- Rollback: `git revert` the Phase 1 commit brings v1 back.

## 8. Risks

- The private state may also cap the upcoming list. Phase 0 measures it before anything is built on it.
- `set_queue` is covered upstream by offline tests plus a live add/remove check, not a live full replace. Phase 3's read-back is the proof here.
- Restore of a shuffled context cannot reproduce the old shuffle order. The queued songs and the current song are exact; the snapshot's playlist holds the old order.

## 9. Roadmap (trigger-based)

| Item | Ships when |
|---|---|
| One-tap page on a `.ace` name | Ace asks for a phone button |
| Voice: "park my queue" on the satellites | The Spotify Connect voice lane (separate decision, 2026-11-20) is live |

## 10. Acceptance

- [ ] `probe --seed 25` shows 25 or more queued tracks and restores the starting queue. Evidence: command output in the handback.
- [ ] `park` on a live session leaves the current song playing untouched and the queue empty of queued songs, with a playlist whose count matches. Evidence: before/after `probe` output and the playlist link.
- [ ] `restore` returns the saved song, position (within 5 s) and queued songs in order. Evidence: `probe` output against the snapshot.
- [ ] All section 3 invariants have a passing test. Evidence: `uv run pytest -q`.
