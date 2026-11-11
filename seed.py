"""Populate a demo database:  python seed.py   (resets instance/campusloop.db)"""
import io
import os
import random
import shutil
import sys
from datetime import timedelta

from PIL import Image, ImageDraw
from werkzeug.security import generate_password_hash

import campus_data
import services as S
import vision


class FS:  # minimal stand-in for werkzeug FileStorage
    def __init__(self, b):
        self.stream = io.BytesIO(b)

    def read(self):
        return self.stream.read()


def fake_photo(label, seed):
    """Synthetic 'evidence' picture (clearly labelled DEMO) so the demo needs no real photos."""
    rnd = random.Random(seed)
    img = Image.new("RGB", (640, 420), tuple(rnd.randint(40, 200) for _ in range(3)))
    d = ImageDraw.Draw(img)
    for _ in range(14):
        x, y = rnd.randint(0, 560), rnd.randint(0, 340)
        d.rectangle([x, y, x + rnd.randint(30, 200), y + rnd.randint(30, 150)],
                    fill=tuple(rnd.randint(0, 255) for _ in range(3)))
    d.rectangle([0, 360, 640, 420], fill=(20, 20, 40))
    d.text((14, 380), f"DEMO PHOTO · {label}", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return FS(buf.getvalue())


def seed(app, reset=True):
    from db import init_db
    path, up = app.config["DATABASE"], app.config["UPLOAD_DIR"]
    if reset:
        if os.path.exists(path):
            os.remove(path)
        shutil.rmtree(up, ignore_errors=True)
    init_db(path)
    os.makedirs(up, exist_ok=True)
    import sqlite3
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    hashes = []
    counter = [0]

    def photo(label, near=None):
        for _ in range(50):
            counter[0] += 1
            try:
                n, h = vision.validate_and_save(fake_photo(label, counter[0] * 7919), up, hashes, near)
                hashes.append(h)
                return n, h
            except vision.PhotoError:
                continue
        raise RuntimeError("could not synthesise photo")

    def user(name, email, pw, role="student", hs="none", block=None, room=None, proof=False):
        pf = photo("hostel allotment letter")[0] if proof else None
        cur = db.execute("INSERT INTO users(name,email,password_hash,role,hostel_status,hostel_block,room_no,hostel_proof,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (name, email, generate_password_hash(pw), role, hs, block, room, pf, S.ts()))
        db.commit()
        return db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()

    admin = user("Campus Admin", "admin@campus.edu", "admin123", "admin")
    U = {}
    for r in campus_data.roster():   # everyone in the roster except two people left to demo self-registration
        if r["email"] in ("priya@campus.edu", "tarun@campus.edu"):
            continue
        U[r["name"].split()[0].lower()] = user(r["name"], r["email"], "student123", hs="verified" if r["hostel_block"] else "none",
                                               block=r["hostel_block"] or None, room=r["hostel_room"] or None)
    meera, vikram, sana, neha, arjun = (U[k] for k in ("meera", "vikram", "sana", "neha", "arjun"))
    aarav, diya, isha, rohan = (U[k] for k in ("aarav", "diya", "isha", "rohan"))

    def back(cid, days):  # time-travel for realistic ages
        t = S.ts(S.now() - timedelta(days=days))
        db.execute("UPDATE complaints SET created_at=?, updated_at=? WHERE id=?", (t, t, cid))
        db.execute("UPDATE activity SET created_at=? WHERE complaint_id=?", (t, cid))
        db.commit()

    def new(u, module, cat, title, desc, loc, need_photo=True, force=False, days=0, pstatus="verified"):
        ph, h = photo(title[:30]) if need_photo else (None, None)
        cid = S.create_complaint(db, u, module, cat, title, desc, loc, ph, h, force=force, photo_status=pstatus)
        if days:
            back(cid, days)
        return cid

    def resolve(cid, note="Fixed and tested."):
        before = db.execute("SELECT photo_hash FROM complaints WHERE id=?", (cid,)).fetchone()[0]
        n, h = photo("AFTER: " + note[:20], near=before)
        S.submit_resolution(db, cid, admin, note, n, h)

    # ---- history: a fixed issue that later becomes "recurring"
    old = new(meera, "classroom", "Electrical", "Tubelight flickering in Room 105",
              "The tubelight keeps flickering and gives a headache during lectures.", "Academic Block C, Room 105", days=21)
    resolve(old, "Replaced tubelight and starter.")
    for u in (meera,):
        S.verify(db, old, u, "fixed")
    db.execute("UPDATE complaints SET resolved_at=? WHERE id=?", (S.ts(S.now() - timedelta(days=18)), old))
    db.commit()

    # ---- classroom
    proj = new(meera, "classroom", "Projector / AV", "Projector not turning on in LH-301",
               "The projector in lecture hall 301 does not turn on. Our class has had no slides for two days and the exam is near.",
               "Academic Block B, Lecture Hall 301", days=2)
    for u in (vikram, sana, neha):
        S.add_support(db, proj, u)
    new(arjun, "classroom", "Projector / AV", "Projector in LH 301 not working",
        "Projector not working in lecture hall 301, screen stays blank.", "Academic Block B, Lecture Hall 301", force=True, days=1)
    new(sana, "classroom", "Electrical", "Ceiling fan sparking in Room 105",
        "The ceiling fan is making sparks and a burning smell. It feels unsafe to sit under it.", "Academic Block C, Room 105", days=1)
    new(vikram, "classroom", "Furniture", "Broken benches in Room 204",
        "Three benches have broken legs.", "Academic Block B, Room 204", days=5, pstatus="unchecked")
    new(neha, "classroom", "Wi-Fi / Network", "Wi-Fi very slow in Computer Lab 2",
        "Internet is extremely slow during lab sessions, pages do not load.", "Lab Complex, Computer Lab 2", need_photo=False, days=3)

    # ---- hostel
    water = new(aarav, "hostel", "Water supply", "No water supply on 2nd floor Block A",
                "There is no water in the washrooms on the second floor since morning. Over 30 students affected.",
                "Block A, Floor 2 Washroom", need_photo=False, days=1)
    for u in (diya, isha):
        S.add_support(db, water, u)
    new(rohan, "hostel", "Plumbing / Leakage", "Washroom tap leaking in Block B",
        "A tap in the ground floor washroom keeps dripping and the floor is wet.", "Block B, Ground Floor Washroom", days=4)
    new(isha, "hostel", "Pest control", "Cockroaches in mess hall kitchen area",
        "Cockroaches spotted near the serving counter in the mess hall during dinner.", "Common Areas, Mess Hall", days=2)
    corr = new(diya, "hostel", "Electrical", "Corridor light not working Block A 3rd floor",
               "The corridor light on the third floor of Block A is dead. It is very dark at night.", "Block A, Floor 3 Corridor", days=3)
    S.add_support(db, corr, aarav)
    resolve(corr, "Replaced the LED panel; tested.")          # → students must verify
    mess = new(aarav, "hostel", "Mess / Food quality", "Stale food served at dinner",
               "The rice and curry served at dinner smelled stale and several people felt sick.", "Common Areas, Mess Hall", days=6)
    resolve(mess, "Kitchen cleaned and staff briefed.")
    S.verify(db, mess, aarav, "not_fixed", "Same stale smell again today.")   # → re-opened, priority up
    S.recompute_open(db)
    db.commit()
    db.close()


if __name__ == "__main__":
    from app import create_app
    a = create_app()
    seed(a)
    print("Seeded. Admin: admin@campus.edu / admin123   Students: <name>@campus.edu / student123")
