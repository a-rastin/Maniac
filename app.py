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
        # Required for ON DELETE CASCADE between playlists and playlist_songs.
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
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

            CREATE TABLE IF NOT EXISTS playlists (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS playlist_songs (
                playlist_id INTEGER NOT NULL,
                song_id     INTEGER NOT NULL,
                position    INTEGER NOT NULL,
                added_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (playlist_id, song_id),
                FOREIGN KEY (playlist_id) REFERENCES playlists(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (song_id) REFERENCES songs(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_playlist_songs_order
                ON playlist_songs(playlist_id, position);
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


def _coerce_id_list(payload) -> list[int]:
    """Pull a list of positive ints out of `{"ids": [...]}` JSON payloads."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("ids")
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out.append(value)
    return out


@app.route("/api/songs", methods=["DELETE"])
def delete_songs():
    """Remove songs from the library catalog.

    Per the product decision, the underlying MP3 files in `uploads/` are
    intentionally left on disk; only the database rows (and any playlist
    membership, via ON DELETE CASCADE) are removed.
    """
    ids = _coerce_id_list(request.get_json(silent=True))
    if not ids:
        return jsonify({"error": "No song ids provided"}), 400

    db = get_db()
    placeholders = ",".join("?" for _ in ids)
    cur = db.execute(
        f"DELETE FROM songs WHERE id IN ({placeholders})", ids
    )
    db.commit()
    return jsonify({"ok": True, "deleted": cur.rowcount})


# --- Playlists --------------------------------------------------------------

@app.route("/api/playlists")
def list_playlists():
    rows = (
        get_db()
        .execute(
            """
            SELECT p.id, p.name,
                   COALESCE(COUNT(ps.song_id), 0) AS song_count
              FROM playlists p
              LEFT JOIN playlist_songs ps ON ps.playlist_id = p.id
             GROUP BY p.id
             ORDER BY p.id DESC
            """
        )
        .fetchall()
    )
    return jsonify([dict(r) for r in rows])


@app.route("/api/playlists", methods=["POST"])
def create_playlist():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Playlist name is required"}), 400
    if len(name) > 100:
        return jsonify({"error": "Playlist name is too long"}), 400

    db = get_db()
    cur = db.execute("INSERT INTO playlists (name) VALUES (?)", (name,))
    db.commit()
    return jsonify({"id": cur.lastrowid, "name": name, "song_count": 0})


@app.route("/api/playlists/<int:playlist_id>", methods=["DELETE"])
def delete_playlist(playlist_id: int):
    db = get_db()
    cur = db.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "Playlist not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/playlists/<int:playlist_id>/songs")
def playlist_songs(playlist_id: int):
    db = get_db()
    exists = db.execute(
        "SELECT 1 FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if exists is None:
        return jsonify({"error": "Playlist not found"}), 404

    q = (request.args.get("q") or "").strip()
    sql = (
        "SELECT s.id, s.title, s.artist, s.filename, s.play_count "
        "  FROM playlist_songs ps "
        "  JOIN songs s ON s.id = ps.song_id "
        " WHERE ps.playlist_id = ?"
    )
    params: list = [playlist_id]
    if q:
        sql += " AND (s.title LIKE ? OR s.artist LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like])
    sql += " ORDER BY ps.position ASC, ps.added_at ASC"

    rows = db.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/playlists/<int:playlist_id>/songs", methods=["POST"])
def add_songs_to_playlist(playlist_id: int):
    ids = _coerce_id_list(request.get_json(silent=True))
    if not ids:
        return jsonify({"error": "No song ids provided"}), 400

    db = get_db()
    exists = db.execute(
        "SELECT 1 FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if exists is None:
        return jsonify({"error": "Playlist not found"}), 404

    next_pos_row = db.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 AS next_pos "
        "  FROM playlist_songs WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()
    next_pos = int(next_pos_row["next_pos"])

    added = 0
    skipped = 0
    for song_id in ids:
        song_row = db.execute(
            "SELECT 1 FROM songs WHERE id = ?", (song_id,)
        ).fetchone()
        if song_row is None:
            skipped += 1
            continue
        try:
            db.execute(
                "INSERT INTO playlist_songs (playlist_id, song_id, position) "
                "VALUES (?, ?, ?)",
                (playlist_id, song_id, next_pos),
            )
            next_pos += 1
            added += 1
        except sqlite3.IntegrityError:
            # Already in this playlist; treat as a skip rather than an error.
            skipped += 1
    db.commit()
    return jsonify({"ok": True, "added": added, "skipped": skipped})


@app.route("/api/playlists/<int:playlist_id>/songs", methods=["DELETE"])
def remove_songs_from_playlist(playlist_id: int):
    ids = _coerce_id_list(request.get_json(silent=True))
    if not ids:
        return jsonify({"error": "No song ids provided"}), 400

    db = get_db()
    exists = db.execute(
        "SELECT 1 FROM playlists WHERE id = ?", (playlist_id,)
    ).fetchone()
    if exists is None:
        return jsonify({"error": "Playlist not found"}), 404

    placeholders = ",".join("?" for _ in ids)
    cur = db.execute(
        f"DELETE FROM playlist_songs "
        f"WHERE playlist_id = ? AND song_id IN ({placeholders})",
        [playlist_id, *ids],
    )
    db.commit()
    return jsonify({"ok": True, "removed": cur.rowcount})


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

  /* Library + playlists list (sidebar) */
  .nav-list { display: flex; flex-direction: column; gap: 4px; }
  .nav-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 9px 12px;
    border-radius: 9px;
    background: transparent;
    border: 1px solid transparent;
    color: var(--text-bright);
    cursor: pointer;
    text-align: left;
    font-size: 14px;
    font-weight: 500;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
  }
  .nav-item:hover { background: var(--surface-elev); border-color: var(--border); }
  .nav-item.active {
    background: linear-gradient(135deg,
                  var(--active-grad-1), var(--active-grad-2));
    border-color: var(--active-border);
    color: var(--text-bright);
  }
  .nav-item .label-text { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .nav-item .count-badge {
    font-size: 11px;
    color: var(--text-label);
    font-variant-numeric: tabular-nums;
  }
  .nav-item .icon-btn {
    width: 22px;
    height: 22px;
    border-radius: 5px;
    background: transparent;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    display: none;
    align-items: center;
    justify-content: center;
    padding: 0;
    font-size: 14px;
    line-height: 1;
  }
  .nav-item:hover .icon-btn { display: inline-flex; }
  .nav-item .icon-btn:hover { background: var(--surface-elev-hover); color: var(--status-error); }

  .new-playlist-row { display: flex; gap: 6px; }
  .new-playlist-row input {
    flex: 1;
    background: var(--surface-elev);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 10px;
    color: var(--text);
    font-size: 13px;
  }
  .new-playlist-row input:focus {
    outline: none;
    border-color: var(--accent-1);
    box-shadow: 0 0 0 3px var(--accent-1-glow);
  }
  .new-playlist-row .btn-mini {
    background: var(--surface-elev);
    border: 1px solid var(--border);
    color: var(--text-bright);
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 13px;
    cursor: pointer;
    font-weight: 600;
  }
  .new-playlist-row .btn-mini:hover {
    background: var(--surface-elev-hover);
    border-color: var(--border-strong);
  }

  /* Content header (above song list) */
  .content-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 0 0 16px 0;
  }
  .content-header h2 { margin: 0; flex: 1; }
  .header-btn {
    background: var(--surface-elev);
    border: 1px solid var(--border);
    color: var(--text-bright);
    padding: 8px 14px;
    border-radius: 8px;
    cursor: pointer;
    font-weight: 600;
    font-size: 13px;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
  }
  .header-btn:hover {
    background: var(--surface-elev-hover);
    border-color: var(--border-strong);
  }
  .header-btn.danger:hover {
    border-color: var(--status-error);
    color: var(--status-error);
  }
  .header-btn.active {
    background: linear-gradient(135deg, var(--accent-1), var(--accent-2));
    border-color: transparent;
    color: var(--text-on-accent);
  }

  /* Action bar shown in select mode */
  .action-bar {
    display: none;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    margin-bottom: 14px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 11px;
  }
  .action-bar.visible { display: flex; }
  .action-bar .count {
    font-weight: 600;
    color: var(--text-bright);
    margin-right: 8px;
  }
  .action-bar .spacer { flex: 1; }
  .action-bar button {
    background: var(--surface-elev);
    border: 1px solid var(--border);
    color: var(--text-bright);
    padding: 7px 12px;
    border-radius: 8px;
    cursor: pointer;
    font-weight: 600;
    font-size: 13px;
  }
  .action-bar button:hover {
    background: var(--surface-elev-hover);
    border-color: var(--border-strong);
  }
  .action-bar button.danger { color: var(--status-error); }
  .action-bar button.danger:hover { border-color: var(--status-error); }
  .action-bar button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  /* Selection checkbox on song row */
  .song .checkbox {
    width: 18px;
    height: 18px;
    border: 1.5px solid var(--border-strong);
    border-radius: 5px;
    display: none;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    color: var(--text-on-accent);
    background: var(--surface-elev);
  }
  body.select-mode .song .checkbox { display: inline-flex; }
  body.select-mode .song .checkbox svg { display: none; }
  .song.selected .checkbox {
    background: linear-gradient(135deg, var(--accent-1), var(--accent-2));
    border-color: transparent;
  }
  .song.selected .checkbox svg { display: block; width: 12px; height: 12px; }
  .song.selected {
    border-color: var(--active-border);
    background: linear-gradient(135deg,
                  var(--active-grad-1), var(--active-grad-2));
  }

  /* Dialog / modal */
  .dialog-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 50;
  }
  :root[data-theme="light"] .dialog-backdrop {
    background: rgba(0, 0, 0, 0.35);
  }
  .dialog-backdrop.visible { display: flex; }
  .dialog {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 22px;
    min-width: 320px;
    max-width: 460px;
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.45);
  }
  .dialog h3 {
    margin: 0 0 10px 0;
    font-size: 17px;
    font-weight: 700;
    color: var(--text-bright);
  }
  .dialog p {
    margin: 0 0 16px 0;
    color: var(--text-muted);
    font-size: 14px;
    line-height: 1.5;
  }
  .dialog-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 16px;
  }
  .dialog-actions button {
    background: var(--surface-elev);
    border: 1px solid var(--border);
    color: var(--text-bright);
    padding: 9px 16px;
    border-radius: 8px;
    cursor: pointer;
    font-weight: 600;
    font-size: 13px;
  }
  .dialog-actions button:hover {
    background: var(--surface-elev-hover);
    border-color: var(--border-strong);
  }
  .dialog-actions .danger {
    color: var(--text-on-accent);
    background: #dc2626;
    border-color: transparent;
  }
  .dialog-actions .danger:hover { background: #b91c1c; border-color: transparent; }
  .dialog-actions .primary {
    background: linear-gradient(135deg, var(--accent-1), var(--accent-2));
    border-color: transparent;
    color: var(--text-on-accent);
  }
  .dialog-actions .primary:hover { filter: brightness(1.06); }

  .playlist-picker { display: flex; flex-direction: column; gap: 6px; max-height: 280px; overflow-y: auto; margin-bottom: 12px; }
  .picker-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    background: var(--surface-elev);
    border: 1px solid var(--border);
    border-radius: 9px;
    cursor: pointer;
    color: var(--text-bright);
    font-size: 14px;
    text-align: left;
    width: 100%;
    font-family: inherit;
  }
  .picker-item:hover {
    background: var(--surface-elev-hover);
    border-color: var(--accent-1);
  }
  .picker-item .label-text { flex: 1; }
  .picker-item .count-badge {
    font-size: 12px;
    color: var(--text-label);
  }
  .picker-empty {
    color: var(--text-empty);
    font-size: 13px;
    padding: 16px;
    text-align: center;
  }
  .dialog input[type=text].dialog-input {
    width: 100%;
    background: var(--surface-elev);
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 10px 12px;
    color: var(--text);
    font-size: 14px;
  }
  .dialog input[type=text].dialog-input:focus {
    outline: none;
    border-color: var(--accent-1);
    box-shadow: 0 0 0 3px var(--accent-1-glow);
  }

  .hidden { display: none !important; }

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

      <div class="section" id="view-tabs-section">
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

      <div class="section">
        <div class="label">Library</div>
        <div class="nav-list">
          <button class="nav-item active" id="library-nav">
            <span class="label-text">All Songs</span>
          </button>
        </div>
      </div>

      <div class="section">
        <div class="label">Playlists</div>
        <div class="new-playlist-row">
          <input type="text" id="new-playlist-name"
                 placeholder="New playlist name" maxlength="100" />
          <button class="btn-mini" id="create-playlist-btn">Add</button>
        </div>
        <div class="status" id="playlist-status"></div>
        <div class="nav-list" id="playlist-list"></div>
      </div>
    </aside>

    <main class="content">
      <div class="content-header">
        <h2 id="list-heading">All songs</h2>
        <button class="header-btn danger hidden" id="delete-playlist-btn">
          Delete Playlist
        </button>
        <button class="header-btn" id="select-toggle">Select</button>
      </div>

      <div class="action-bar" id="action-bar">
        <span class="count" id="selection-count">0 selected</span>
        <button id="select-all-btn">Select all</button>
        <div class="spacer"></div>
        <button id="add-to-playlist-btn">Add to playlist...</button>
        <button class="danger" id="bulk-delete-btn">Delete</button>
        <button id="cancel-select-btn">Cancel</button>
      </div>

      <div class="song-list" id="song-list"></div>
    </main>
  </div>

  <div class="dialog-backdrop" id="dialog-backdrop">
    <div class="dialog" id="dialog" role="dialog" aria-modal="true">
      <h3 id="dialog-title"></h3>
      <div id="dialog-body"></div>
      <div class="dialog-actions" id="dialog-actions"></div>
    </div>
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
    playlists: [],
    view: { type: "library" },
    currentId: null,
    shuffle: false,
    repeat: false,
    sort: "recent",
    query: "",
    selectMode: false,
    selected: new Set(),
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
  const viewTabsSection = $("view-tabs-section");
  const libraryNav = $("library-nav");
  const playlistList = $("playlist-list");
  const newPlaylistInput = $("new-playlist-name");
  const createPlaylistBtn = $("create-playlist-btn");
  const playlistStatus = $("playlist-status");
  const selectToggleBtn = $("select-toggle");
  const deletePlaylistBtn = $("delete-playlist-btn");
  const actionBar = $("action-bar");
  const selectionCountEl = $("selection-count");
  const selectAllBtn = $("select-all-btn");
  const addToPlaylistBtn = $("add-to-playlist-btn");
  const bulkDeleteBtn = $("bulk-delete-btn");
  const cancelSelectBtn = $("cancel-select-btn");
  const dialogBackdrop = $("dialog-backdrop");
  const dialogTitle = $("dialog-title");
  const dialogBody = $("dialog-body");
  const dialogActions = $("dialog-actions");
  const root = document.documentElement;
  const body = document.body;

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[c]);
  }

  // ---- Data fetching ----

  async function fetchSongs() {
    let url;
    if (state.view.type === "library") {
      const params = new URLSearchParams({
        q: state.query, sort: state.sort,
      });
      url = "/api/songs?" + params.toString();
    } else {
      const params = new URLSearchParams({ q: state.query });
      url = "/api/playlists/" + state.view.id + "/songs?" + params.toString();
    }
    let res;
    try {
      res = await fetch(url);
    } catch (_) {
      songList.innerHTML = '<div class="empty">Failed to load songs.</div>';
      return;
    }
    if (!res.ok) {
      if (res.status === 404 && state.view.type === "playlist") {
        state.view = { type: "library" };
        await fetchPlaylists();
        await fetchSongs();
        renderHeader();
        return;
      }
      songList.innerHTML = '<div class="empty">Failed to load songs.</div>';
      return;
    }
    state.songs = await res.json();
    pruneSelection();
    renderSongs();
    renderSelectionBar();
  }

  async function fetchPlaylists() {
    try {
      const res = await fetch("/api/playlists");
      state.playlists = res.ok ? await res.json() : [];
    } catch (_) {
      state.playlists = [];
    }
    renderPlaylists();
  }

  // ---- Render ----

  function renderHeader() {
    if (state.view.type === "library") {
      listHeading.textContent =
        state.sort === "most_played" ? "Most played" : "All songs";
      deletePlaylistBtn.classList.add("hidden");
      viewTabsSection.classList.remove("hidden");
    } else {
      listHeading.textContent = state.view.name;
      deletePlaylistBtn.classList.remove("hidden");
      viewTabsSection.classList.add("hidden");
    }
  }

  function renderPlaylists() {
    libraryNav.classList.toggle("active", state.view.type === "library");

    if (state.playlists.length === 0) {
      playlistList.innerHTML =
        '<div class="empty" style="padding: 12px; font-size: 12px;">'
        + 'No playlists yet.</div>';
      return;
    }

    const frag = document.createDocumentFragment();
    state.playlists.forEach((p) => {
      const isActive =
        state.view.type === "playlist" && state.view.id === p.id;
      const btn = document.createElement("button");
      btn.className = "nav-item" + (isActive ? " active" : "");
      btn.innerHTML =
        '<span class="label-text">' + escapeHtml(p.name) + '</span>'
        + '<span class="count-badge">' + p.song_count + '</span>'
        + '<span class="icon-btn" title="Delete playlist" '
        +   'aria-label="Delete playlist">&times;</span>';
      btn.addEventListener("click", (e) => {
        if (e.target.classList.contains("icon-btn")) {
          e.stopPropagation();
          deletePlaylist(p.id, p.name);
          return;
        }
        openPlaylist(p.id, p.name);
      });
      frag.appendChild(btn);
    });
    playlistList.innerHTML = "";
    playlistList.appendChild(frag);
  }

  function renderSongs() {
    if (state.songs.length === 0) {
      let msg;
      if (state.query) {
        msg = "No songs match your search.";
      } else if (state.view.type === "playlist") {
        msg = "This playlist is empty. Open the library, "
            + "select songs, and add them here.";
      } else {
        msg = "No songs yet. Upload an MP3 from the sidebar to get started.";
      }
      songList.innerHTML = '<div class="empty">' + escapeHtml(msg) + "</div>";
      return;
    }

    const checkSvg =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
      + 'stroke-width="3" stroke-linecap="round" stroke-linejoin="round">'
      + '<polyline points="20 6 9 17 4 12"></polyline></svg>';

    const frag = document.createDocumentFragment();
    state.songs.forEach((song, idx) => {
      const isSelected = state.selected.has(song.id);
      const div = document.createElement("div");
      div.className = "song"
        + (song.id === state.currentId ? " active" : "")
        + (isSelected ? " selected" : "");
      div.innerHTML =
        '<div class="checkbox">' + checkSvg + '</div>'
        + '<div class="num">' + (idx + 1) + '</div>'
        + '<div class="info">'
        +   '<div class="title">' + escapeHtml(song.title) + '</div>'
        +   '<div class="artist">' + escapeHtml(song.artist) + '</div>'
        + '</div>'
        + '<div class="count">' + song.play_count
        + (song.play_count === 1 ? " play" : " plays") + '</div>';
      div.addEventListener("click", () => {
        if (state.selectMode) {
          toggleSelection(song.id);
        } else {
          playSong(song.id);
        }
      });
      frag.appendChild(div);
    });
    songList.innerHTML = "";
    songList.appendChild(frag);
  }

  function renderSelectionBar() {
    if (!state.selectMode) {
      actionBar.classList.remove("visible");
      return;
    }
    actionBar.classList.add("visible");
    const n = state.selected.size;
    selectionCountEl.textContent = n + " selected";
    const noneSelected = n === 0;
    bulkDeleteBtn.disabled = noneSelected;
    addToPlaylistBtn.disabled = noneSelected;

    if (state.view.type === "playlist") {
      bulkDeleteBtn.textContent = "Remove from playlist";
    } else {
      bulkDeleteBtn.textContent = "Delete";
    }
  }

  // ---- Selection ----

  function pruneSelection() {
    const visible = new Set(state.songs.map((s) => s.id));
    for (const id of Array.from(state.selected)) {
      if (!visible.has(id)) state.selected.delete(id);
    }
  }

  function toggleSelection(id) {
    if (state.selected.has(id)) state.selected.delete(id);
    else state.selected.add(id);
    renderSongs();
    renderSelectionBar();
  }

  function setSelectMode(on) {
    state.selectMode = !!on;
    body.classList.toggle("select-mode", state.selectMode);
    selectToggleBtn.classList.toggle("active", state.selectMode);
    selectToggleBtn.textContent = state.selectMode ? "Done" : "Select";
    if (!state.selectMode) state.selected.clear();
    renderSongs();
    renderSelectionBar();
  }

  function selectAllVisible() {
    state.songs.forEach((s) => state.selected.add(s.id));
    renderSongs();
    renderSelectionBar();
  }

  // ---- Views ----

  function openLibrary() {
    state.view = { type: "library" };
    state.query = "";
    if (searchInput) searchInput.value = "";
    setSelectMode(false);
    renderHeader();
    renderPlaylists();
    fetchSongs();
  }

  function openPlaylist(id, name) {
    state.view = { type: "playlist", id: id, name: name };
    state.query = "";
    if (searchInput) searchInput.value = "";
    setSelectMode(false);
    renderHeader();
    renderPlaylists();
    fetchSongs();
  }

  // ---- Playlist operations ----

  async function createPlaylist(name) {
    name = (name || "").trim();
    if (!name) {
      playlistStatus.textContent = "Please enter a name.";
      playlistStatus.className = "status error";
      return;
    }
    try {
      const res = await fetch("/api/playlists", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        playlistStatus.textContent = data.error || "Failed to create playlist.";
        playlistStatus.className = "status error";
        return;
      }
      newPlaylistInput.value = "";
      playlistStatus.textContent = 'Created "' + data.name + '".';
      playlistStatus.className = "status success";
      await fetchPlaylists();
    } catch (err) {
      playlistStatus.textContent = "Failed to create playlist.";
      playlistStatus.className = "status error";
    }
  }

  function deletePlaylist(id, name) {
    showConfirm({
      title: "Delete playlist?",
      message: 'This removes the playlist "' + name + '". '
             + "The songs in your library are not deleted.",
      confirmLabel: "Delete playlist",
      danger: true,
      onConfirm: async () => {
        const res = await fetch("/api/playlists/" + id, { method: "DELETE" });
        if (!res.ok) {
          playlistStatus.textContent = "Failed to delete playlist.";
          playlistStatus.className = "status error";
          return;
        }
        if (state.view.type === "playlist" && state.view.id === id) {
          openLibrary();
        } else {
          await fetchPlaylists();
        }
      },
    });
  }

  function deleteCurrentPlaylist() {
    if (state.view.type !== "playlist") return;
    deletePlaylist(state.view.id, state.view.name);
  }

  async function bulkDelete() {
    const ids = Array.from(state.selected);
    if (ids.length === 0) return;

    if (state.view.type === "playlist") {
      const plId = state.view.id;
      const plName = state.view.name;
      showConfirm({
        title: "Remove from playlist?",
        message:
          "Remove " + ids.length + " song"
          + (ids.length === 1 ? "" : "s")
          + ' from "' + plName + '"? '
          + "The songs will remain in your library.",
        confirmLabel: "Remove",
        danger: false,
        onConfirm: async () => {
          const res = await fetch(
            "/api/playlists/" + plId + "/songs",
            {
              method: "DELETE",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ ids: ids }),
            }
          );
          if (!res.ok) return;
          setSelectMode(false);
          await fetchPlaylists();
          await fetchSongs();
        },
      });
    } else {
      showConfirm({
        title: "Delete from library?",
        message:
          "Delete " + ids.length + " song"
          + (ids.length === 1 ? "" : "s")
          + " from the library? "
          + "This also removes them from any playlists they belong to. "
          + "The underlying MP3 files in uploads/ are kept on disk.",
        confirmLabel: "Delete",
        danger: true,
        onConfirm: async () => {
          const res = await fetch("/api/songs", {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ids: ids }),
          });
          if (!res.ok) return;
          // If the currently playing song was deleted, clear the player.
          if (state.currentId != null && ids.indexOf(state.currentId) !== -1) {
            stopSong();
            audio.removeAttribute("src");
            audio.load();
            state.currentId = null;
            npTitle.textContent = "Nothing playing";
            npArtist.textContent = "Pick a song from the list";
          }
          setSelectMode(false);
          await fetchPlaylists();
          await fetchSongs();
        },
      });
    }
  }

  function openAddToPlaylistPicker() {
    const ids = Array.from(state.selected);
    if (ids.length === 0) return;

    let candidates = state.playlists;
    if (state.view.type === "playlist") {
      candidates = candidates.filter((p) => p.id !== state.view.id);
    }

    const list = document.createElement("div");
    list.className = "playlist-picker";
    if (candidates.length === 0) {
      const empty = document.createElement("div");
      empty.className = "picker-empty";
      empty.textContent = "No other playlists. Create one first.";
      list.appendChild(empty);
    } else {
      candidates.forEach((p) => {
        const btn = document.createElement("button");
        btn.className = "picker-item";
        btn.innerHTML =
          '<span class="label-text">' + escapeHtml(p.name) + '</span>'
          + '<span class="count-badge">' + p.song_count + ' song'
          + (p.song_count === 1 ? "" : "s") + '</span>';
        btn.addEventListener("click", () => {
          addSongsToPlaylist(p.id, p.name, ids);
        });
        list.appendChild(btn);
      });
    }

    const newRow = document.createElement("div");
    newRow.className = "new-playlist-row";
    newRow.innerHTML =
      '<input type="text" class="dialog-input" '
      + 'placeholder="New playlist name" maxlength="100" />'
      + '<button class="btn-mini">Create</button>';
    const input = newRow.querySelector("input");
    const createBtn = newRow.querySelector("button");
    const createAndAdd = async () => {
      const name = input.value.trim();
      if (!name) {
        input.focus();
        return;
      }
      try {
        const res = await fetch("/api/playlists", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          alert(data.error || "Failed to create playlist.");
          return;
        }
        await addSongsToPlaylist(data.id, data.name, ids);
      } catch (_) {
        alert("Failed to create playlist.");
      }
    };
    createBtn.addEventListener("click", createAndAdd);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        createAndAdd();
      }
    });

    showDialog({
      title: "Add to playlist",
      message: "Add " + ids.length + " song"
             + (ids.length === 1 ? "" : "s") + " to:",
      content: [list, newRow],
      buttons: [{ label: "Cancel", onClick: closeDialog }],
    });
  }

  async function addSongsToPlaylist(playlistId, playlistName, ids) {
    try {
      const res = await fetch("/api/playlists/" + playlistId + "/songs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: ids }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        alert(data.error || "Failed to add songs to playlist.");
        return;
      }
      closeDialog();
      setSelectMode(false);
      playlistStatus.textContent =
        "Added " + data.added + " to \"" + playlistName + "\""
        + (data.skipped ? " (" + data.skipped + " already in playlist)" : "")
        + ".";
      playlistStatus.className = "status success";
      await fetchPlaylists();
      if (
        state.view.type === "playlist" && state.view.id === playlistId
      ) {
        await fetchSongs();
      }
    } catch (_) {
      alert("Failed to add songs to playlist.");
    }
  }

  // ---- Dialog ----

  function showDialog({ title, message, content, buttons }) {
    dialogTitle.textContent = title || "";
    dialogBody.innerHTML = "";
    if (message) {
      const p = document.createElement("p");
      p.textContent = message;
      dialogBody.appendChild(p);
    }
    if (Array.isArray(content)) {
      content.forEach((node) => dialogBody.appendChild(node));
    } else if (content instanceof Node) {
      dialogBody.appendChild(content);
    }
    dialogActions.innerHTML = "";
    (buttons || []).forEach((b) => {
      const btn = document.createElement("button");
      btn.textContent = b.label;
      if (b.className) btn.className = b.className;
      btn.addEventListener("click", () => {
        if (typeof b.onClick === "function") b.onClick();
      });
      dialogActions.appendChild(btn);
    });
    dialogBackdrop.classList.add("visible");
  }

  function showConfirm({ title, message, confirmLabel, danger, onConfirm }) {
    showDialog({
      title: title,
      message: message,
      buttons: [
        { label: "Cancel", onClick: closeDialog },
        {
          label: confirmLabel || "Confirm",
          className: danger ? "danger" : "primary",
          onClick: () => {
            closeDialog();
            if (typeof onConfirm === "function") onConfirm();
          },
        },
      ],
    });
  }

  function closeDialog() {
    dialogBackdrop.classList.remove("visible");
  }

  dialogBackdrop.addEventListener("click", (e) => {
    if (e.target === dialogBackdrop) closeDialog();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && dialogBackdrop.classList.contains("visible")) {
      closeDialog();
    }
  });

  // ---- Playback ----

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
    // state.songs is scoped to the current view (library or playlist),
    // so Shuffle/Next stay within that scope automatically.
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
      .then((r) => {
        if (r.ok) {
          // Refresh both lists so play_count updates are visible everywhere.
          if (state.view.type === "library") fetchSongs();
        }
      })
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

  // ---- Theme ----

  function currentTheme() {
    return root.getAttribute("data-theme") === "light" ? "light" : "dark";
  }
  function applyTheme(theme) {
    if (theme === "light") root.setAttribute("data-theme", "light");
    else root.removeAttribute("data-theme");
    try { localStorage.setItem("maniac-theme", theme); } catch (_) {}
  }
  themeToggle.addEventListener("click", () => {
    applyTheme(currentTheme() === "light" ? "dark" : "light");
  });

  // ---- Search ----

  let searchTimer;
  searchInput.addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const val = e.target.value;
    searchTimer = setTimeout(() => {
      state.query = val;
      fetchSongs();
    }, 200);
  });

  // ---- Sort tabs (library only) ----

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      if (state.view.type !== "library") return;
      tabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      state.sort = tab.dataset.sort;
      renderHeader();
      fetchSongs();
    });
  });

  // ---- Library / playlist nav ----

  libraryNav.addEventListener("click", openLibrary);

  // ---- New playlist creation (sidebar) ----

  createPlaylistBtn.addEventListener("click", () => {
    createPlaylist(newPlaylistInput.value);
  });
  newPlaylistInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      createPlaylist(newPlaylistInput.value);
    }
  });

  // ---- Select mode + bulk actions ----

  selectToggleBtn.addEventListener("click", () => setSelectMode(!state.selectMode));
  cancelSelectBtn.addEventListener("click", () => setSelectMode(false));
  selectAllBtn.addEventListener("click", selectAllVisible);
  bulkDeleteBtn.addEventListener("click", bulkDelete);
  addToPlaylistBtn.addEventListener("click", openAddToPlaylistPicker);
  deletePlaylistBtn.addEventListener("click", deleteCurrentPlaylist);

  // ---- Upload ----

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
        if (state.view.type === "library") {
          await fetchSongs();
        }
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

  // ---- Initial load ----

  renderHeader();
  fetchPlaylists();
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
