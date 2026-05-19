"""
Maniac - Music Player Web App (Python + Flask)

A single-file Flask application that serves a browser-based music player on
localhost. Supports MP3 uploads, search by title/artist, a most-played list,
shuffle mode, repeat mode, a Stop button, a Next-song button, and a
dark/light theme toggle in the lower-left corner.

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
<script>
  (function () {
    try {
      var saved = localStorage.getItem("maniac-theme");
      if (saved === "light" || saved === "dark") {
        document.documentElement.setAttribute("data-theme", saved);
      }
    } catch (_) { /* localStorage unavailable; fall back to default */ }
  })();
</script>
<style>
  :root {
    --bg: #0b0b11;
    --surface: #14141d;
    --surface-elev: #1c1c28;
    --surface-elev-hover: #232333;
    --surface-hover: #191923;
    --upload-hover: #1f1f2e;
    --border-soft: #232330;
    --border: #2a2a3a;
    --border-strong: #3a3a55;
    --tab-active: #2a2a3d;
    --text: #e8e8ec;
    --text-bright: #d8d8df;
    --text-muted: #9999a8;
    --text-label: #8a8a99;
    --text-faint: #6a6a78;
    --text-dim: #5d5d6d;
    --text-empty: #5a5a68;
    --text-on-accent: #ffffff;
    --accent-1: #a78bfa;
    --accent-2: #ec4899;
    --accent-1-glow: rgba(167, 139, 250, 0.18);
    --active-grad-1: rgba(167, 139, 250, 0.16);
    --active-grad-2: rgba(236, 72, 153, 0.10);
    --active-border: rgba(167, 139, 250, 0.55);
    --status-info: #93c5fd;
    --status-success: #86efac;
    --status-error: #fca5a5;
    --audio-filter: invert(0.9) hue-rotate(180deg);
    --tab-active-shadow: none;
  }

  :root[data-theme="light"] {
    --bg: #f5f5f7;
    --surface: #ffffff;
    --surface-elev: #eceef3;
    --surface-elev-hover: #dfe1e8;
    --surface-hover: #f1f2f6;
    --upload-hover: #f7f8fb;
    --border-soft: #e6e7ed;
    --border: #d9dae2;
    --border-strong: #b9bac6;
    --tab-active: #ffffff;
    --text: #1d1d22;
    --text-bright: #0b0b11;
    --text-muted: #686877;
    --text-label: #686877;
    --text-faint: #9c9ca6;
    --text-dim: #9c9ca6;
    --text-empty: #b3b5c0;
    --text-on-accent: #ffffff;
    --accent-1: #7c3aed;
    --accent-2: #db2777;
    --accent-1-glow: rgba(124, 58, 237, 0.18);
    --active-grad-1: rgba(124, 58, 237, 0.10);
    --active-grad-2: rgba(219, 39, 119, 0.06);
    --active-border: rgba(124, 58, 237, 0.45);
    --status-info: #2563eb;
    --status-success: #15803d;
    --status-error: #b91c1c;
    --audio-filter: none;
    --tab-active-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  }

  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 Oxygen, Ubuntu, Cantarell, "Open Sans", "Helvetica Neue", sans-serif;
    background: var(--bg);
    color: var(--text);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    transition: background-color 0.2s, color 0.2s;
  }
  .app { display: flex; flex: 1; min-height: 0; }

  /* Sidebar */
  .sidebar {
    width: 320px;
    background: var(--surface);
    padding: 24px 22px;
    display: flex;
    flex-direction: column;
    gap: 22px;
    border-right: 1px solid var(--border-soft);
    overflow-y: auto;
  }
  .brand {
    margin: 0 0 4px 0;
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.01em;
    background: linear-gradient(135deg, var(--accent-1), var(--accent-2));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }
  .section { display: flex; flex-direction: column; gap: 8px; }
  .section .label {
    font-size: 11px;
    font-weight: 700;
    color: var(--text-label);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .upload-box {
    position: relative;
    background: var(--surface-elev);
    border: 1px dashed var(--border-strong);
    border-radius: 10px;
    padding: 14px;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
  }
  .upload-box:hover { border-color: var(--accent-1); background: var(--upload-hover); }
  .upload-box input[type=file] {
    position: absolute; inset: 0; opacity: 0; cursor: pointer;
  }
  .upload-box .hint { color: var(--text-muted); font-size: 13px; }
  .upload-box .sub { color: var(--text-dim); font-size: 11px; margin-top: 4px; }

  .status { font-size: 12px; min-height: 16px; }
  .status.info { color: var(--status-info); }
  .status.success { color: var(--status-success); }
  .status.error { color: var(--status-error); }

  input[type=text], input.search {
    width: 100%;
    background: var(--surface-elev);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 11px 12px;
    color: var(--text);
    font-size: 14px;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  input.search:focus {
    outline: none;
    border-color: var(--accent-1);
    box-shadow: 0 0 0 3px var(--accent-1-glow);
  }

  .tabs {
    display: flex;
    gap: 4px;
    background: var(--surface-elev);
    border-radius: 10px;
    padding: 4px;
  }
  .tab {
    flex: 1;
    background: transparent;
    border: none;
    color: var(--text-muted);
    padding: 8px 10px;
    border-radius: 7px;
    cursor: pointer;
    font-weight: 600;
    font-size: 13px;
    transition: background 0.15s, color 0.15s;
  }
  .tab:hover { color: var(--text-bright); }
  .tab.active {
    background: var(--tab-active);
    color: var(--text-bright);
    box-shadow: var(--tab-active-shadow);
  }

  .toggle {
    background: var(--surface-elev);
    border: 1px solid var(--border);
    color: var(--text-bright);
    padding: 11px 14px;
    border-radius: 10px;
    cursor: pointer;
    font-weight: 600;
    font-size: 14px;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
    text-align: left;
  }
  .toggle:hover { border-color: var(--border-strong); }
  .toggle.on {
    background: linear-gradient(135deg, var(--accent-1), var(--accent-2));
    border-color: transparent;
    color: var(--text-on-accent);
  }

  /* Content */
  .content { flex: 1; overflow-y: auto; padding: 24px 32px 32px; }
  .content h2 {
    margin: 0 0 16px 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-bright);
  }
  .song-list { display: flex; flex-direction: column; gap: 6px; }
  .song {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 12px 16px;
    background: var(--surface);
    border-radius: 11px;
    cursor: pointer;
    border: 1px solid transparent;
    transition: background 0.15s, border-color 0.15s, transform 0.05s;
  }
  .song:hover { background: var(--surface-hover); border-color: var(--border); }
  .song:active { transform: translateY(1px); }
  .song.active {
    background: linear-gradient(135deg,
                  var(--active-grad-1),
                  var(--active-grad-2));
    border-color: var(--active-border);
  }
  .song .num {
    width: 28px;
    color: var(--text-faint);
    font-variant-numeric: tabular-nums;
    text-align: right;
  }
  .song.active .num { color: var(--accent-1); }
  .song .info { flex: 1; min-width: 0; }
  .song .title {
    font-weight: 600;
    font-size: 15px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .song .artist {
    color: var(--text-muted);
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .song .count {
    color: var(--text-label);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    background: var(--surface-elev);
    padding: 4px 8px;
    border-radius: 999px;
  }
  .song.active .count { color: var(--text-bright); }

  .empty {
    color: var(--text-empty);
    text-align: center;
    padding: 60px 20px;
    font-size: 14px;
  }

  /* Player bar */
  .player-bar {
    background: var(--surface);
    border-top: 1px solid var(--border-soft);
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
    color: var(--text-muted);
    font-size: 12px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  audio {
    flex: 1;
    min-width: 0;
    height: 38px;
    filter: var(--audio-filter);
  }
  .pb-btn {
    background: var(--surface-elev);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 10px 16px;
    border-radius: 9px;
    cursor: pointer;
    font-weight: 600;
    font-size: 13px;
    min-width: 76px;
    transition: background 0.15s, border-color 0.15s, transform 0.05s;
  }
  .pb-btn:hover { background: var(--surface-elev-hover); border-color: var(--border-strong); }
  .pb-btn:active { transform: translateY(1px); }
  .pb-btn.primary {
    background: linear-gradient(135deg, var(--accent-1), var(--accent-2));
    border-color: transparent;
    color: var(--text-on-accent);
  }
  .pb-btn.primary:hover { filter: brightness(1.06); }

  /* Theme toggle (anchored to the lower-left corner of the app) */
  .theme-toggle {
    background: var(--surface-elev);
    border: 1px solid var(--border);
    color: var(--text);
    width: 40px;
    height: 40px;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.15s, border-color 0.15s, transform 0.05s, color 0.15s;
  }
  .theme-toggle:hover {
    background: var(--surface-elev-hover);
    border-color: var(--border-strong);
    color: var(--accent-1);
  }
  .theme-toggle:active { transform: translateY(1px); }
  .theme-toggle svg { width: 18px; height: 18px; display: block; }
  .theme-toggle .icon-sun { display: block; }
  .theme-toggle .icon-moon { display: none; }
  :root[data-theme="light"] .theme-toggle .icon-sun { display: none; }
  :root[data-theme="light"] .theme-toggle .icon-moon { display: block; }

  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 999px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--border-strong); }
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
        <button class="toggle" id="repeat-toggle">Repeat: Off</button>
      </div>
    </aside>

    <main class="content">
      <h2 id="list-heading">All songs</h2>
      <div class="song-list" id="song-list"></div>
    </main>
  </div>

  <footer class="player-bar">
    <button class="theme-toggle" id="theme-toggle"
            title="Toggle light/dark theme" aria-label="Toggle theme">
      <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
           aria-hidden="true">
        <circle cx="12" cy="12" r="4"/>
        <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>
      </svg>
      <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
           aria-hidden="true">
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
      </svg>
    </button>
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
    repeat: false,
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
  const repeatToggle = $("repeat-toggle");
  const themeToggle = $("theme-toggle");
  const btnStop = $("btn-stop");
  const btnNext = $("btn-next");
  const tabs = document.querySelectorAll(".tab");
  const root = document.documentElement;

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

  audio.addEventListener("ended", () => {
    if (state.repeat && state.currentId != null) {
      try { audio.currentTime = 0; } catch (_) { /* no source yet */ }
      state.playCounted = false;
      audio.play().catch(() => {});
      return;
    }
    nextSong();
  });

  btnNext.addEventListener("click", nextSong);
  btnStop.addEventListener("click", stopSong);

  shuffleToggle.addEventListener("click", () => {
    state.shuffle = !state.shuffle;
    shuffleToggle.textContent =
      "Shuffle: " + (state.shuffle ? "On" : "Off");
    shuffleToggle.classList.toggle("on", state.shuffle);
  });

  repeatToggle.addEventListener("click", () => {
    state.repeat = !state.repeat;
    repeatToggle.textContent =
      "Repeat: " + (state.repeat ? "On" : "Off");
    repeatToggle.classList.toggle("on", state.repeat);
  });

  function currentTheme() {
    return root.getAttribute("data-theme") === "light" ? "light" : "dark";
  }
  function applyTheme(theme) {
    if (theme === "light") {
      root.setAttribute("data-theme", "light");
    } else {
      root.removeAttribute("data-theme");
    }
    try { localStorage.setItem("maniac-theme", theme); } catch (_) {}
  }
  themeToggle.addEventListener("click", () => {
    applyTheme(currentTheme() === "light" ? "dark" : "light");
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
