"""
Maniac - Music Player Web App (Python + Flask)

A single-file Flask application that serves a browser-based music player on
localhost. Supports MP3 uploads, search by title/artist, a most-played list,
shuffle mode, a Stop button, and a Next-song button.

Run with:

    pip install -r requirements.txt
    python app.py

Then open http://127.0.0.1:5000 (the script also tries to open it for you).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
import webbrowser
from pathlib import Path

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    render_template_string,
    request,
    send_from_directory,
)
from mutagen import File as MutagenFile
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "songs.db"
UPLOAD_DIR = BASE_DIR / "uploads"
ALLOWED_EXTENSIONS = {".mp3"}
MAX_UPLOAD_BYTES = 64 * 1024 * 1024  # 64 MB cap per file

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask("Maniac")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS songs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT    NOT NULL,
                artist      TEXT    NOT NULL,
                filename    TEXT    NOT NULL UNIQUE,
                play_count  INTEGER NOT NULL DEFAULT 0,
                uploaded_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


# ----------------------------------------------------------------------------
# Metadata extraction
# ----------------------------------------------------------------------------

def extract_tags(filepath: Path, fallback_name: str) -> tuple[str, str]:
    """Return (title, artist), reading ID3 tags when available."""
    title: str | None = None
    artist: str | None = None
    try:
        audio = MutagenFile(str(filepath), easy=True)
        if audio is not None:
            title_vals = audio.get("title")
            artist_vals = audio.get("artist")
            if title_vals:
                title = str(title_vals[0])
            if artist_vals:
                artist = str(artist_vals[0])
    except Exception:
        # Malformed file or missing tag block; fall back to defaults below.
        pass

    if not title:
        title = Path(fallback_name).stem or "Untitled"
    if not artist:
        artist = "Unknown Artist"
    return title.strip(), artist.strip()


# ----------------------------------------------------------------------------
# Routes - API
# ----------------------------------------------------------------------------

@app.route("/api/upload", methods=["POST"])
def upload_song():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    original = secure_filename(f.filename) or "song.mp3"
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "Only MP3 files are accepted"}), 400

    stored_name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / stored_name
    f.save(dest)

    title, artist = extract_tags(dest, original)

    db = get_db()
    cur = db.execute(
        "INSERT INTO songs (title, artist, filename) VALUES (?, ?, ?)",
        (title, artist, stored_name),
    )
    db.commit()
    return jsonify(
        {
            "id": cur.lastrowid,
            "title": title,
            "artist": artist,
            "filename": stored_name,
            "play_count": 0,
        }
    )


@app.route("/api/songs")
def list_songs():
    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort") or "recent"
    order_clause = (
        "play_count DESC, id DESC" if sort == "most_played" else "id DESC"
    )

    sql = "SELECT id, title, artist, filename, play_count FROM songs"
    params: list = []
    if q:
        sql += " WHERE title LIKE ? OR artist LIKE ?"
        like = f"%{q}%"
        params.extend([like, like])
    sql += f" ORDER BY {order_clause}"

    rows = get_db().execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/songs/<int:song_id>/played", methods=["POST"])
def increment_play_count(song_id: int):
    db = get_db()
    cur = db.execute(
        "UPDATE songs SET play_count = play_count + 1 WHERE id = ?",
        (song_id,),
    )
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "Song not found"}), 404
    return jsonify({"ok": True})


@app.route("/audio/<int:song_id>")
def stream_audio(song_id: int):
    row = (
        get_db()
        .execute("SELECT filename FROM songs WHERE id = ?", (song_id,))
        .fetchone()
    )
    if row is None:
        return jsonify({"error": "Song not found"}), 404
    # conditional=True enables HTTP Range support so audio seeking works.
    return send_from_directory(UPLOAD_DIR, row["filename"], conditional=True)


# ----------------------------------------------------------------------------
# Routes - UI
# ----------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Maniac</title>
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 Oxygen, Ubuntu, Cantarell, "Open Sans", "Helvetica Neue", sans-serif;
    background: #0b0b11;
    color: #e8e8ec;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .app { display: flex; flex: 1; min-height: 0; }

  /* Sidebar */
  .sidebar {
    width: 320px;
    background: #14141d;
    padding: 24px 22px;
    display: flex;
    flex-direction: column;
    gap: 22px;
    border-right: 1px solid #232330;
    overflow-y: auto;
  }
  .brand {
    margin: 0 0 4px 0;
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.01em;
    background: linear-gradient(135deg, #a78bfa, #ec4899);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }
  .section { display: flex; flex-direction: column; gap: 8px; }
  .section .label {
    font-size: 11px;
    font-weight: 700;
    color: #8a8a99;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .upload-box {
    position: relative;
    background: #1c1c28;
    border: 1px dashed #3a3a55;
    border-radius: 10px;
    padding: 14px;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
  }
  .upload-box:hover { border-color: #a78bfa; background: #1f1f2e; }
  .upload-box input[type=file] {
    position: absolute; inset: 0; opacity: 0; cursor: pointer;
  }
  .upload-box .hint { color: #9999a8; font-size: 13px; }
  .upload-box .sub { color: #5d5d6d; font-size: 11px; margin-top: 4px; }

  .status { font-size: 12px; min-height: 16px; }
  .status.info { color: #93c5fd; }
  .status.success { color: #86efac; }
  .status.error { color: #fca5a5; }

  input[type=text], input.search {
    width: 100%;
    background: #1c1c28;
    border: 1px solid #2a2a3a;
    border-radius: 10px;
    padding: 11px 12px;
    color: #e8e8ec;
    font-size: 14px;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  input.search:focus {
    outline: none;
    border-color: #a78bfa;
    box-shadow: 0 0 0 3px rgba(167, 139, 250, 0.18);
  }

  .tabs {
    display: flex;
    gap: 4px;
    background: #1c1c28;
    border-radius: 10px;
    padding: 4px;
  }
  .tab {
    flex: 1;
    background: transparent;
    border: none;
    color: #9999a8;
    padding: 8px 10px;
    border-radius: 7px;
    cursor: pointer;
    font-weight: 600;
    font-size: 13px;
    transition: background 0.15s, color 0.15s;
  }
  .tab:hover { color: #d8d8df; }
  .tab.active { background: #2a2a3d; color: #fff; }

  .toggle {
    background: #1c1c28;
    border: 1px solid #2a2a3a;
    color: #d8d8df;
    padding: 11px 14px;
    border-radius: 10px;
    cursor: pointer;
    font-weight: 600;
    font-size: 14px;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
    text-align: left;
  }
  .toggle:hover { border-color: #3a3a55; }
  .toggle.on {
    background: linear-gradient(135deg, #a78bfa, #ec4899);
    border-color: transparent;
    color: #fff;
  }

  /* Content */
  .content { flex: 1; overflow-y: auto; padding: 24px 32px 32px; }
  .content h2 {
    margin: 0 0 16px 0;
    font-size: 18px;
    font-weight: 600;
    color: #d8d8df;
  }
  .song-list { display: flex; flex-direction: column; gap: 6px; }
  .song {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 12px 16px;
    background: #14141d;
    border-radius: 11px;
    cursor: pointer;
    border: 1px solid transparent;
    transition: background 0.15s, border-color 0.15s, transform 0.05s;
  }
  .song:hover { background: #191923; border-color: #2a2a3a; }
  .song:active { transform: translateY(1px); }
  .song.active {
    background: linear-gradient(135deg,
                  rgba(167, 139, 250, 0.16),
                  rgba(236, 72, 153, 0.10));
    border-color: rgba(167, 139, 250, 0.55);
  }
  .song .num {
    width: 28px;
    color: #6a6a78;
    font-variant-numeric: tabular-nums;
    text-align: right;
  }
  .song.active .num { color: #a78bfa; }
  .song .info { flex: 1; min-width: 0; }
  .song .title {
    font-weight: 600;
    font-size: 15px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .song .artist {
    color: #9999a8;
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .song .count {
    color: #8a8a99;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    background: #1c1c28;
    padding: 4px 8px;
    border-radius: 999px;
  }
  .song.active .count { color: #d8d8df; }

  .empty {
    color: #5a5a68;
    text-align: center;
    padding: 60px 20px;
    font-size: 14px;
  }

  /* Player bar */
  .player-bar {
    background: #14141d;
    border-top: 1px solid #232330;
    padding: 14px 22px;
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .now-playing { min-width: 220px; max-width: 280px; overflow: hidden; }
  .now-playing .np-title {
    font-weight: 600;
    font-size: 14px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .now-playing .np-artist {
    color: #9999a8;
    font-size: 12px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  audio {
    flex: 1;
    min-width: 0;
    height: 38px;
    filter: invert(0.9) hue-rotate(180deg);
  }
  .pb-btn {
    background: #1c1c28;
    border: 1px solid #2a2a3a;
    color: #e8e8ec;
    padding: 10px 16px;
    border-radius: 9px;
    cursor: pointer;
    font-weight: 600;
    font-size: 13px;
    min-width: 76px;
    transition: background 0.15s, border-color 0.15s, transform 0.05s;
  }
  .pb-btn:hover { background: #232333; border-color: #3a3a55; }
  .pb-btn:active { transform: translateY(1px); }
  .pb-btn.primary {
    background: linear-gradient(135deg, #a78bfa, #ec4899);
    border-color: transparent;
    color: #fff;
  }
  .pb-btn.primary:hover { filter: brightness(1.06); }

  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb { background: #2a2a3a; border-radius: 999px; }
  ::-webkit-scrollbar-thumb:hover { background: #3a3a55; }
  ::-webkit-scrollbar-track { background: transparent; }
</style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <h1 class="brand">Maniac</h1>

      <div class="section">
        <div class="label">Add a song</div>
        <label class="upload-box" id="upload-box">
          <input type="file" id="upload" accept="audio/mpeg,.mp3" />
          <div class="hint">Click or drop an MP3 here</div>
          <div class="sub">Max 64 MB</div>
        </label>
        <div class="status" id="upload-status"></div>
      </div>

      <div class="section">
        <div class="label">Search</div>
        <input class="search" id="search" type="text"
               placeholder="Song or artist..." autocomplete="off" />
      </div>

      <div class="section">
        <div class="label">View</div>
        <div class="tabs" role="tablist">
          <button class="tab active" data-sort="recent">All</button>
          <button class="tab" data-sort="most_played">Most Played</button>
        </div>
      </div>

      <div class="section">
        <div class="label">Playback</div>
        <button class="toggle" id="shuffle-toggle">Shuffle: Off</button>
      </div>
    </aside>

    <main class="content">
      <h2 id="list-heading">All songs</h2>
      <div class="song-list" id="song-list"></div>
    </main>
  </div>

  <footer class="player-bar">
    <div class="now-playing">
      <div class="np-title" id="np-title">Nothing playing</div>
      <div class="np-artist" id="np-artist">Pick a song from the list</div>
    </div>
    <audio id="audio" controls preload="none"></audio>
    <button class="pb-btn" id="btn-stop">Stop</button>
    <button class="pb-btn primary" id="btn-next">Next</button>
  </footer>

<script>
(function () {
  const state = {
    songs: [],
    currentId: null,
    shuffle: false,
    sort: "recent",
    query: "",
    playCounted: false,
  };

  const $ = (id) => document.getElementById(id);
  const songList = $("song-list");
  const listHeading = $("list-heading");
  const audio = $("audio");
  const npTitle = $("np-title");
  const npArtist = $("np-artist");
  const searchInput = $("search");
  const uploadInput = $("upload");
  const uploadStatus = $("upload-status");
  const shuffleToggle = $("shuffle-toggle");
  const btnStop = $("btn-stop");
  const btnNext = $("btn-next");
  const tabs = document.querySelectorAll(".tab");

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[c]);
  }

  async function fetchSongs() {
    const params = new URLSearchParams({ q: state.query, sort: state.sort });
    const res = await fetch("/api/songs?" + params.toString());
    if (!res.ok) {
      songList.innerHTML =
        '<div class="empty">Failed to load songs.</div>';
      return;
    }
    state.songs = await res.json();
    renderSongs();
  }

  function renderSongs() {
    listHeading.textContent =
      state.sort === "most_played" ? "Most played" : "All songs";

    if (state.songs.length === 0) {
      const msg = state.query
        ? "No songs match your search."
        : "No songs yet. Upload an MP3 from the sidebar to get started.";
      songList.innerHTML = '<div class="empty">' + escapeHtml(msg) + "</div>";
      return;
    }

    const frag = document.createDocumentFragment();
    state.songs.forEach((song, idx) => {
      const div = document.createElement("div");
      div.className =
        "song" + (song.id === state.currentId ? " active" : "");
      div.innerHTML =
        '<div class="num">' + (idx + 1) + "</div>" +
        '<div class="info">' +
          '<div class="title">' + escapeHtml(song.title) + "</div>" +
          '<div class="artist">' + escapeHtml(song.artist) + "</div>" +
        "</div>" +
        '<div class="count">' + song.play_count +
        (song.play_count === 1 ? " play" : " plays") + "</div>";
      div.addEventListener("click", () => playSong(song.id));
      frag.appendChild(div);
    });
    songList.innerHTML = "";
    songList.appendChild(frag);
  }

  function playSong(id) {
    const song = state.songs.find((s) => s.id === id);
    if (!song) return;
    state.currentId = id;
    state.playCounted = false;
    audio.src = "/audio/" + id;
    npTitle.textContent = song.title;
    npArtist.textContent = song.artist;
    renderSongs();
    audio.play().catch(() => {
      // Autoplay rejection or load error - user can retry from controls.
    });
  }

  function nextSong() {
    if (state.songs.length === 0) return;
    if (state.shuffle) {
      const candidates = state.songs.filter(
        (s) => s.id !== state.currentId
      );
      const pool = candidates.length > 0 ? candidates : state.songs;
      const pick = pool[Math.floor(Math.random() * pool.length)];
      playSong(pick.id);
    } else {
      const idx = state.songs.findIndex((s) => s.id === state.currentId);
      const nextIdx = idx === -1 ? 0 : (idx + 1) % state.songs.length;
      playSong(state.songs[nextIdx].id);
    }
  }

  function stopSong() {
    audio.pause();
    try { audio.currentTime = 0; } catch (_) { /* no source yet */ }
  }

  audio.addEventListener("play", () => {
    if (state.playCounted || state.currentId == null) return;
    state.playCounted = true;
    fetch("/api/songs/" + state.currentId + "/played", { method: "POST" })
      .then((r) => { if (r.ok) return fetchSongs(); })
      .catch(() => {});
  });

  audio.addEventListener("ended", nextSong);

  btnNext.addEventListener("click", nextSong);
  btnStop.addEventListener("click", stopSong);

  shuffleToggle.addEventListener("click", () => {
    state.shuffle = !state.shuffle;
    shuffleToggle.textContent =
      "Shuffle: " + (state.shuffle ? "On" : "Off");
    shuffleToggle.classList.toggle("on", state.shuffle);
  });

  let searchTimer;
  searchInput.addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const val = e.target.value;
    searchTimer = setTimeout(() => {
      state.query = val;
      fetchSongs();
    }, 200);
  });

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      state.sort = tab.dataset.sort;
      fetchSongs();
    });
  });

  uploadInput.addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;

    uploadStatus.textContent = "Uploading " + file.name + "...";
    uploadStatus.className = "status info";

    const form = new FormData();
    form.append("file", file);

    try {
      const res = await fetch("/api/upload", { method: "POST", body: form });
      let data = {};
      try { data = await res.json(); } catch (_) {}
      if (res.ok) {
        uploadStatus.textContent =
          "Added: " + data.title + " - " + data.artist;
        uploadStatus.className = "status success";
        uploadInput.value = "";
        await fetchSongs();
      } else {
        uploadStatus.textContent =
          data.error || ("Upload failed (HTTP " + res.status + ")");
        uploadStatus.className = "status error";
      }
    } catch (err) {
      uploadStatus.textContent = "Upload failed: " + err.message;
      uploadStatus.className = "status error";
    }
  });

  fetchSongs();
})();
</script>
</body>
</html>
"""


@app.route("/")
def index() -> Response:
    # No Jinja variables are used; render_template_string keeps everything
    # in this file as planned without requiring a templates/ folder.
    return Response(render_template_string(INDEX_HTML), mimetype="text/html")


# ----------------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------------

def _open_browser_when_ready(url: str) -> None:
    """Open the default browser shortly after the server is up."""

    def _runner() -> None:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Timer(1.0, _runner).start()


def main() -> None:
    init_db()
    host = os.environ.get("MUSIC_PLAYER_HOST", "127.0.0.1")
    port = int(os.environ.get("MUSIC_PLAYER_PORT", "5000"))
    # Only open the browser in the main process, not in the reloader child.
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        _open_browser_when_ready(f"http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
