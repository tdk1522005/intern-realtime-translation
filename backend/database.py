
import sqlite3
from pathlib import Path

DATABASE_PATH = Path(__file__).resolve().parent / "translation.db"


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS presentations (
                presentation_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'active',
                started_at TEXT NOT NULL,
                ended_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                presentation_id TEXT NOT NULL,
                session_number INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                started_at TEXT NOT NULL,
                ended_at TEXT,
                FOREIGN KEY (presentation_id)
                    REFERENCES presentations(presentation_id),
                UNIQUE (presentation_id, session_number)
            );

            CREATE TABLE IF NOT EXISTS segments (
                segment_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                sequence_number INTEGER NOT NULL,
                source_text TEXT NOT NULL,
                source_language TEXT NOT NULL
                    CHECK (source_language IN ('en', 'vi')),
                en TEXT,
                vn TEXT,
                status TEXT NOT NULL DEFAULT 'received',
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id)
                    REFERENCES sessions(session_id),
                UNIQUE (session_id, sequence_number)
            );
            """
        )


if __name__ == "__main__":
    init_db()
    print(f"Database initialized: {DATABASE_PATH}")
