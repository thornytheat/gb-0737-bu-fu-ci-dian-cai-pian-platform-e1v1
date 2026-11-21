import json
import sqlite3
from contextlib import closing, contextmanager

from app.manifest import canonical
from app.reasons import FabricError


class Archive:
    def __init__(self, path):
        self.path = path

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def setup(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, time TEXT NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews (
                    seq INTEGER PRIMARY KEY, run TEXT REFERENCES runs(id) NOT NULL, time TEXT NOT NULL, action TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shift_sheets (
                    id TEXT PRIMARY KEY, run TEXT REFERENCES runs(id) NOT NULL, version INTEGER NOT NULL,
                    active INTEGER NOT NULL, time TEXT NOT NULL, document TEXT NOT NULL,
                    UNIQUE(run, version)
                );
                CREATE TABLE IF NOT EXISTS shift_events (
                    seq INTEGER PRIMARY KEY, sheet TEXT REFERENCES shift_sheets(id) NOT NULL,
                    time TEXT NOT NULL, action TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS recent_runs ON runs(time DESC, id DESC);
                CREATE INDEX IF NOT EXISTS active_sheet ON shift_sheets(run, active);
            """)

    @contextmanager
    def write(self):
        try:
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        except sqlite3.OperationalError as exc:
            if (getattr(exc, "sqlite_errorcode", 0) & 255) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise FabricError("STORE_BUSY", 503) from exc
            raise

    @contextmanager
    def reader(self):
        with closing(self.connect()) as db:
            yield db

    def read(self, run_id):
        with closing(self.connect()) as db:
            document = lookup(db, run_id)
        if document is None:
            raise FabricError("RUN_UNKNOWN", 404)
        return document

    def recent(self):
        with closing(self.connect()) as db:
            return [json.loads(row["manifest"]) for row in db.execute(
                "SELECT json_extract(document, '$.manifest') AS manifest FROM runs ORDER BY time DESC,id DESC LIMIT 30",
            )]

    def active_shift_sheet(self, db, run_id):
        row = db.execute(
            "SELECT document FROM shift_sheets WHERE run=? AND active=1 ORDER BY version DESC LIMIT 1", (run_id,),
        ).fetchone()
        return json.loads(row["document"]) if row else None

    def shift_sheet(self, db, sheet_id):
        row = db.execute("SELECT document FROM shift_sheets WHERE id=?", (sheet_id,)).fetchone()
        if row is None:
            raise FabricError("SHEET_UNKNOWN", 404)
        return json.loads(row["document"])

    def shift_history(self, db, run_id):
        rows = db.execute(
            "SELECT document FROM shift_sheets WHERE run=? ORDER BY version", (run_id,),
        ).fetchall()
        return [json.loads(row["document"]) for row in rows]


def lookup(db, run_id):
    row = db.execute("SELECT document FROM runs WHERE id=?", (run_id,)).fetchone()
    return json.loads(row["document"]) if row else None


def save(db, document):
    manifest = document["manifest"]
    db.execute("INSERT INTO runs VALUES (?,?,?)", (manifest["id"], manifest["created_at"], canonical(document)))


def review(db, run_id, time, action):
    db.execute("INSERT INTO reviews (run,time,action) VALUES (?,?,?)", (run_id, time, action))


def save_sheet(db, document):
    manifest = document["manifest"]
    db.execute("UPDATE shift_sheets SET active=0 WHERE run=?", (manifest["run"],))
    db.execute("INSERT INTO shift_sheets (id,run,version,active,time,document) VALUES (?,?,?,?,?,?)",
               (manifest["id"], manifest["run"], manifest["version"], 1, manifest["created_at"], canonical(document)))
    db.execute("INSERT INTO shift_events (sheet,time,action) VALUES (?,?,?)",
               (manifest["id"], manifest["created_at"], "published"))


def sheet_event(db, sheet_id, time, action):
    db.execute("INSERT INTO shift_events (sheet,time,action) VALUES (?,?,?)", (sheet_id, time, action))
