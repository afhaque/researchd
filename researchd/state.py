"""SQLite state: seen-URL dedup, past queries, run records."""

import sqlite3
from pathlib import Path

from .util import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_sources (
    url TEXT NOT NULL,
    mission TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    run_id TEXT NOT NULL,
    PRIMARY KEY (url, mission)
);
CREATE TABLE IF NOT EXISTS queries (
    mission TEXT NOT NULL,
    qid INTEGER NOT NULL,
    query TEXT NOT NULL,
    run_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    mission TEXT NOT NULL,
    started TEXT NOT NULL,
    ended TEXT,
    status TEXT,
    questions_done INTEGER DEFAULT 0,
    sources_ingested INTEGER DEFAULT 0
);
"""


class State:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def is_seen(self, mission: str, url: str) -> bool:
        row = self.db.execute(
            'SELECT 1 FROM seen_sources WHERE mission=? AND url=?',
            (mission, url)).fetchone()
        return row is not None

    def mark_seen(self, mission: str, url: str, run_id: str) -> None:
        self.db.execute(
            'INSERT OR IGNORE INTO seen_sources VALUES (?, ?, ?, ?)',
            (url, mission, now_iso(), run_id))
        self.db.commit()

    def past_queries(self, mission: str, qid: int) -> list[str]:
        rows = self.db.execute(
            'SELECT query FROM queries WHERE mission=? AND qid=?',
            (mission, qid)).fetchall()
        return [r[0] for r in rows]

    def record_query(self, mission: str, qid: int, query: str,
                     run_id: str) -> None:
        self.db.execute('INSERT INTO queries VALUES (?, ?, ?, ?)',
                        (mission, qid, query, run_id))
        self.db.commit()

    def start_run(self, run_id: str, mission: str) -> None:
        self.db.execute(
            'INSERT INTO runs (run_id, mission, started) VALUES (?, ?, ?)',
            (run_id, mission, now_iso()))
        self.db.commit()

    def end_run(self, run_id: str, status: str, questions_done: int,
                sources_ingested: int) -> None:
        self.db.execute(
            'UPDATE runs SET ended=?, status=?, questions_done=?, '
            'sources_ingested=? WHERE run_id=?',
            (now_iso(), status, questions_done, sources_ingested, run_id))
        self.db.commit()
