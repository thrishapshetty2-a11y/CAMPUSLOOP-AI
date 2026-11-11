import os
import sqlite3
from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'student',              -- student | admin
  hostel_status TEXT NOT NULL DEFAULT 'none',        -- none | pending | verified | rejected
  hostel_block TEXT, room_no TEXT, hostel_proof TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS complaints(
  id INTEGER PRIMARY KEY, module TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL,
  description TEXT NOT NULL, location TEXT NOT NULL, photo TEXT, photo_hash TEXT,
  photo_status TEXT NOT NULL DEFAULT 'none',          -- none | verified | unchecked | flagged
  photo_note TEXT,
  reporter_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'open',               -- open | in_progress | pending_verification | resolved
  priority_score REAL NOT NULL DEFAULT 0, priority_label TEXT NOT NULL DEFAULT 'Low', priority_why TEXT,
  related_to INTEGER REFERENCES complaints(id), link_score REAL,
  recurring INTEGER NOT NULL DEFAULT 0, reopen_count INTEGER NOT NULL DEFAULT 0,
  verify_deadline TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS supporters(
  complaint_id INTEGER NOT NULL, user_id INTEGER NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(complaint_id, user_id));
CREATE TABLE IF NOT EXISTS resolutions(
  id INTEGER PRIMARY KEY, complaint_id INTEGER NOT NULL, admin_id INTEGER NOT NULL,
  note TEXT NOT NULL, photo TEXT NOT NULL, photo_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verifications(
  id INTEGER PRIMARY KEY, complaint_id INTEGER NOT NULL, resolution_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL, verdict TEXT NOT NULL, comment TEXT, created_at TEXT NOT NULL,
  UNIQUE(resolution_id, user_id));
CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY, complaint_id INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_c_mod ON complaints(module, status);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


def close_db(_=None):
    d = g.pop("db", None)
    if d:
        d.close()


def init_db(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
