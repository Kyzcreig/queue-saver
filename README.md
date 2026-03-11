# Queue Saver

Saves your Spotify queue to a dated playlist, then clears it — so you can start fresh for a party or event.

Single-file HTML app. No server, no build step, no dependencies.

---

## What It Does

1. **Reads your current Spotify queue** (up to 200 tracks)
2. **Creates a private playlist** named `Queue Playlist YYYY/MM/DD HH:MM:SS`
3. **Adds all queued tracks** to the playlist
4. **Optionally clears your queue** (can skip the clear if you just want a snapshot)

Handles queues >20 tracks by using a skip-batch technique to expose tracks Spotify's API hides behind the 20-item queue window.

---

## Setup

### 1. Create a Spotify App

1. Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Click **Create app**
3. Set **Redirect URI** to the URL where you're serving the file (e.g. `http://127.0.0.1:5500/index.html` for Live Server, or `file:///Users/you/.../queue-saver/index.html` if opening locally)
4. Copy your **Client ID**

### 2. Configure Client ID

The app ships with a hardcoded default Client ID. To use your own:

- Open the app in your browser
- If the setup banner appears (no Client ID in localStorage), paste your Client ID and click **Save**
- Or edit the fallback in `index.html`:
  ```js
  let CLIENT_ID = localStorage.getItem('qs_client_id') || 'your-client-id-here';
  ```

The Client ID is persisted in `localStorage` — you only need to enter it once per browser.

---

## Usage

1. Open `index.html` in a browser (or serve it locally)
2. Click **Connect with Spotify** — authorizes via PKCE OAuth (no secret needed)
3. Set how many tracks to save (default: 10, max: 200)
4. Toggle **Clear queue after saving** if you want a clean slate
5. Click **Save Queue to Playlist**

The playlist link appears in the status area when done.

---

## Serving Locally

Any static file server works. Quickest options:

```bash
# Python
python3 -m http.server 5500

# Node
npx serve .

# VS Code Live Server extension
# Right-click index.html → "Open with Live Server"
```

The redirect URI in your Spotify app must match exactly — including the port.

---

## OAuth Scopes Used

| Scope | Why |
|---|---|
| `user-read-playback-state` | Check current playback |
| `user-modify-playback-state` | Skip tracks to expose the next queue batch |
| `playlist-modify-public` | Create playlists |
| `playlist-modify-private` | Create private playlists |
| `user-read-currently-playing` | Show now-playing widget |
| `user-read-private` | Get user ID for playlist creation |

---

## Notes

- Spotify's queue API only returns the **next 20 items**. For >20 tracks, the app skips through the queue in batches to reveal the rest — this modifies playback state.
- The "Clear queue" checkbox is auto-forced on when saving >20 tracks (skipping is required to access deeper items).
- Tokens are stored in `localStorage`. Disconnect via the **Disconnect** button to clear them.
- No data leaves your browser except Spotify API calls.
