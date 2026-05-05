"""SQLite history para yt-transcriber. Compartilhado entre CLI e API web."""

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

OUTPUT_DIR = Path(os.environ.get("YT_OUTPUT_DIR", str(Path.home() / "yt-transcricoes")))
DB_PATH = OUTPUT_DIR / "history.db"

log = logging.getLogger("yt_transcriber.db")


def init_db():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            created_at TEXT,
            title TEXT,
            channel TEXT,
            url TEXT,
            summary TEXT,
            md_path TEXT
        )
    """)
    con.commit()
    con.close()


def save_history(job_id: str, results_json: dict, md_path: str) -> int:
    """Insere uma linha por video presente em results_json['results']. Retorna n inseridos."""
    n = 0
    try:
        con = sqlite3.connect(str(DB_PATH))
        for r in results_json.get("results", []):
            info = r.get("info", {})
            con.execute(
                "INSERT INTO history (job_id, created_at, title, channel, url, summary, md_path) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    job_id,
                    datetime.now().isoformat(),
                    info.get("title", ""),
                    info.get("channel", ""),
                    info.get("url", ""),
                    r.get("summary", "")[:300],
                    md_path,
                ),
            )
            n += 1
        con.commit()
        con.close()
    except Exception as e:
        log.warning(f"Falha ao salvar historico: {e}")
    return n


def get_history(q: str = "") -> list:
    try:
        con = sqlite3.connect(str(DB_PATH))
        con.row_factory = sqlite3.Row
        if q:
            rows = con.execute(
                "SELECT * FROM history WHERE title LIKE ? OR summary LIKE ? "
                "ORDER BY created_at DESC LIMIT 50",
                (f"%{q}%", f"%{q}%"),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM history ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []
