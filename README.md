# Maniac

**A lightweight, browser-based music player implemented in Python and Flask**

Maniac is a single-file web application that simulates a local music player in the browser. It provides core playback and library-management functionality—MP3 ingestion, metadata extraction, search, play-count tracking, shuffle playback, and transport controls—without requiring a separate frontend build step or external database server.

---

## Abstract

This project demonstrates a minimal client–server architecture for personal music playback. A Flask backend handles file storage, metadata persistence, and audio streaming, while an embedded HTML5 interface renders the user experience in any modern web browser. The design prioritizes simplicity, local execution, and reproducibility, making it suitable as a reference implementation for introductory coursework in web development, human–computer interaction, or software engineering.

---

## Features

| Feature | Description |
|--------|-------------|
| **MP3 upload** | Add songs via the browser; files are stored locally and indexed in SQLite |
| **Metadata extraction** | ID3 tags (title, artist) are read with Mutagen; missing tags fall back to filename defaults |
| **Search** | Case-insensitive lookup by song title or artist name |
| **Most played list** | Songs ranked by cumulative play count |
| **Shuffle mode** | Random selection from the active song list |
| **Stop control** | Pause playback and reset position to the beginning |
| **Next control** | Advance to the next track (or a random track when shuffle is enabled) |

---

## System Architecture

Maniac follows a three-tier pattern adapted for local deployment:

```
┌─────────────────────────────────────────────────────────┐
│                    Web Browser (Client)                  │
│  HTML5 Audio · Vanilla JavaScript · Embedded CSS         │
└──────────────────────────┬──────────────────────────────┘
                           │ HTTP (REST + static audio)
┌──────────────────────────▼──────────────────────────────┐
│                 Flask Application Server                 │
│  Routes · Upload handling · Metadata parsing · Streaming │
└──────────────┬──────────────────────────┬─────────────────┘
               │                          │
     ┌─────────▼─────────┐      ┌────────▼────────┐
     │   SQLite (songs.db)│      │  uploads/ (MP3) │
     │  Metadata · counts │      │  Binary storage │
     └────────────────────┘      └─────────────────┘
```

**Design decisions:**

- **Single-file delivery:** Application logic, API routes, and frontend markup are consolidated in `app.py` to reduce deployment complexity.
- **SQLite persistence:** Lightweight, file-based storage requires no external database configuration.
- **HTML5 `<audio>` element:** Native browser playback with HTTP Range support for seeking.
- **Play-count semantics:** Each playback session increments the count once on the initial `play` event, avoiding inflation from pause/resume cycles.

---

## Technology Stack

| Layer | Technology | Role |
|-------|------------|------|
| Runtime | Python 3.10+ | Application language |
| Web framework | Flask 3.x | HTTP server and routing |
| Metadata | Mutagen | MP3 tag parsing |
| Database | SQLite3 (stdlib) | Song catalog and play statistics |
| Frontend | HTML5, CSS3, JavaScript | User interface and playback controls |

---

## Requirements

- Python 3.10 or later
- pip (Python package manager)
- A modern web browser with HTML5 audio support

---

## Installation

1. **Clone the repository**

   ```bash
   git clone https://github.com/<username>/maniac.git
   cd maniac
   ```

2. **Create and activate a virtual environment** *(recommended)*

   ```bash
   python -m venv venv

   # Windows
   venv\Scripts\activate

   # macOS / Linux
   source venv/bin/activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

---

## Usage

Start the application from the project directory:

```bash
python app.py
```

The server binds to `http://127.0.0.1:5000` by default and attempts to open the interface in your default browser.

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_PLAYER_HOST` | `127.0.0.1` | Host address for the Flask server |
| `MUSIC_PLAYER_PORT` | `5000` | Port number for the Flask server |

Example:

```bash
set MUSIC_PLAYER_PORT=8080
python app.py
```

### Basic workflow

1. Upload an MP3 file using the sidebar control.
2. Browse or search the song library.
3. Click a track to begin playback.
4. Use **Stop**, **Next**, and **Shuffle** as needed.
5. Switch to the **Most Played** view to inspect listening frequency.

---

## Project Structure

```
maniac/
├── app.py              # Application entry point (backend + frontend)
├── requirements.txt    # Python dependencies
├── songs.db            # SQLite database (created at runtime)
├── uploads/            # Stored MP3 files (created at runtime)
└── README.md           # Project documentation
```

---

## API Reference

All endpoints return JSON unless otherwise noted.

### `GET /`

Serves the single-page user interface.

### `POST /api/upload`

Upload an MP3 file.

- **Content-Type:** `multipart/form-data`
- **Field:** `file` (MP3, max 64 MB)
- **Response:** `{ id, title, artist, filename, play_count }`

### `GET /api/songs`

Retrieve the song catalog.

| Query parameter | Values | Description |
|-----------------|--------|-------------|
| `q` | string | Filter by title or artist (case-insensitive) |
| `sort` | `recent`, `most_played` | Sort order |

**Response:** Array of `{ id, title, artist, filename, play_count }`

### `POST /api/songs/<id>/played`

Increment the play count for a given song.

**Response:** `{ "ok": true }`

### `GET /audio/<id>`

Stream the MP3 file associated with the given song ID. Supports HTTP Range requests for seeking.

---

## Database Schema

```sql
CREATE TABLE songs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    artist      TEXT    NOT NULL,
    filename    TEXT    NOT NULL UNIQUE,
    play_count  INTEGER NOT NULL DEFAULT 0,
    uploaded_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

---

## Limitations

- **Format support:** MP3 only; other audio formats are not accepted.
- **Deployment scope:** Intended for local development and demonstration; Flask's built-in server is not production-grade.
- **Concurrency:** SQLite and local file storage are suitable for single-user scenarios but not optimized for high concurrent load.
- **Authentication:** No user accounts or access control; all uploaded content is globally accessible within the running instance.

---

## Future Work

Potential extensions for academic or research-oriented follow-up:

- Support for additional audio formats (FLAC, OGG, WAV)
- Playlist creation and queue management
- Album artwork extraction and display
- REST API authentication and multi-user libraries
- Migration to a production WSGI server (e.g., Gunicorn, Waitress)
- Automated test suite with pytest and coverage reporting

---

## License

This project is released under the [MIT License](LICENSE). See the `LICENSE` file for details.

---

## Acknowledgments

Built with [Flask](https://flask.palletsprojects.com/) and [Mutagen](https://mutagen.readthedocs.io/). The HTML5 Audio API provides native in-browser playback without proprietary plugins.

---

## Citation

If you use or reference this project in academic work, please cite the repository:

```bibtex
@software{maniac2026,
  title   = {Maniac: A Lightweight Browser-Based Music Player},
  author  = {Your Name},
  year    = {2026},
  url     = {https://github.com/<username>/maniac}
}
```

Replace `<username>` and author details with your own information before publication.
