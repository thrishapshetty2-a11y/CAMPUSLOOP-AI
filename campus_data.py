"""Campus datasets (CSV files in ./data) — the single source of truth.

  data/students.csv  roll_no,name,email,hostel_block,hostel_room   (blank hostel = day scholar)
  data/campus.csv    module,building,place                         (every valid location)

Replace these files with your college's real data. They are re-read automatically when edited.
"""
import csv
import os

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
COMMON = "Common Areas"
_cache = {}


def _load(name):
    path = os.path.join(DATA, name)
    m = os.path.getmtime(path)
    if _cache.get(name, (None, None))[0] != m:
        with open(path, encoding="utf-8-sig", newline="") as f:
            _cache[name] = (m, [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)])
    return _cache[name][1]


def roster():
    return _load("students.csv")


def find_student(email, roll_no=None):
    for r in roster():
        if r["email"].lower() == (email or "").strip().lower():
            if roll_no is None or r["roll_no"].lower() == roll_no.strip().lower():
                return r
    return None


def locations(module, student=None):
    """{building: [places]} the person may report against. Hostel residents: own block + common areas only."""
    out = {}
    for r in _load("campus.csv"):
        if r["module"] == module:
            out.setdefault(r["building"], []).append(r["place"])
    if module == "hostel" and student is not None:
        allowed = {f"Block {student['hostel_block']}", COMMON}
        out = {b: p for b, p in out.items() if b in allowed}
    return out


def valid_location(module, building, place, student=None):
    return place in locations(module, student).get(building, [])


def compose(building, place, room=None):
    if place == "My own room" and room:
        place = f"Room {room}"
    return f"{building}, {place}"
