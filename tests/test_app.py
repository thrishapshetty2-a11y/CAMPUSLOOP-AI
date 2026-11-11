import io
import random
import re
import sys, os

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_engine
from app import create_app
import services as S


def img(seed):
    r = random.Random(seed)
    im = Image.new("RGB", (400, 300), tuple(r.randint(30, 220) for _ in range(3)))
    d = ImageDraw.Draw(im)
    for _ in range(12):
        x, y = r.randint(0, 300), r.randint(0, 220)
        d.rectangle([x, y, x + r.randint(20, 150), y + r.randint(20, 100)], fill=tuple(r.randint(0, 255) for _ in range(3)))
    b = io.BytesIO(); im.save(b, "PNG"); b.seek(0)
    return b


ROSTER = [("T1", "Sam"), ("T2", "Tia"), ("T3", "Amy"), ("T4", "Bob"), ("T5", "Cat"), ("T6", "Out"), ("T7", "Res", "A", "1"), ("T8", "Dan")]
PLACES = {"classroom": {"Block X": ["Room 301", "Room 512", "Room 5", "Hall 9", "Lab 1"]},
          "hostel": {"Block A": ["Floor 1 Washroom", "My own room"], "Block B": ["Floor 1 Washroom"], "Common Areas": ["Mess Hall"]}}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    import campus_data, relevance, csv
    d = tmp_path / "data"; d.mkdir()
    with open(d / "students.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["roll_no", "name", "email", "hostel_block", "hostel_room"])
        for r in ROSTER:
            w.writerow([r[0], r[1], f"{r[1].lower()}@x.edu", r[2] if len(r) > 2 else "", r[3] if len(r) > 3 else ""])
    with open(d / "campus.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["module", "building", "place"])
        for m, bs in PLACES.items():
            for b, ps in bs.items():
                for p in ps:
                    w.writerow([m, b, p])
    monkeypatch.setattr(campus_data, "DATA", str(d)); campus_data._cache.clear()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return create_app(dict(DATABASE=str(tmp_path / "t.db"), UPLOAD_DIR=str(tmp_path / "up"), TESTING=True))


class Client:
    def __init__(self, app):
        self.c = app.test_client()

    def tok(self):
        with self.c.session_transaction() as s:
            if "csrf" not in s:
                s["csrf"] = "tok"
            return s["csrf"]

    def get(self, *a, **k):
        return self.c.get(*a, **k)

    def post(self, url, data=None, **k):
        data = dict(data or {}); data["csrf"] = self.tok()
        return self.c.post(url, data=data, **k)


def register(app, name):
    c = Client(app)
    roll = next(r[0] for r in ROSTER if r[1] == name)
    c.post("/register", dict(roll_no=roll, email=f"{name.lower()}@x.edu", password="secret1"))
    return c


def make_admin(app):
    import sqlite3
    from werkzeug.security import generate_password_hash
    d = sqlite3.connect(app.config["DATABASE"])
    d.execute("INSERT INTO users(name,email,password_hash,role,created_at) VALUES('Admin','admin@x.edu',?,'admin','2026-01-01 00:00:00')",
              (generate_password_hash("adminpw"),)); d.commit(); d.close()
    c = Client(app); c.post("/login", dict(email="admin@x.edu", password="adminpw")); return c


def report(c, seed, module="classroom", cat="Projector / AV", title="Projector not working", desc="The projector in room 301 does not turn on at all.",
           building="Block X", place="Room 301", photo=True, force=False):
    data = dict(category=cat, title=title, description=desc, building=building, place=place)
    if photo:
        data["photo"] = (img(seed), "p.png")
    if force:
        data["force"] = "1"
    return c.post(f"/report/{module}", data, content_type="multipart/form-data", follow_redirects=False)


def hreport(c, seed, **kw):
    base = dict(module="hostel", cat="Plumbing / Leakage", title="Tap leaking", desc="tap leaking in washroom badly", building="Block A", place="Floor 1 Washroom")
    base.update(kw); return report(c, seed, **base)


def cid_from(resp):
    return int(re.search(r"/complaint/(\d+)", resp.headers["Location"]).group(1))


# ------------------------------------------------------------- AI engine
def test_duplicate_detection_and_location_gate():
    new = dict(category="Projector / AV", title="Projector in LH 301 not working", description="screen stays blank", location="Lecture Hall 301")
    same = dict(id=1, category="Projector / AV", title="Projector not turning on in LH-301", description="does not turn on", location="Lecture Hall 301")
    other = dict(same, id=2, location="Lecture Hall 405")
    unrelated = dict(same, id=3, category="Water supply", title="No water in washroom", description="tap dry", location="Lecture Hall 301")
    res = ai_engine.find_similar(new, [same, other, unrelated])
    assert res and res[0][0]["id"] == 1 and res[0][1] >= 0.7
    assert 3 not in [r[0]["id"] for r in res]
    assert all(r[0]["id"] != 2 or r[1] < 0.4 for r in res)


def test_priority_urgency_impact_and_explanations():
    base, _, _ = ai_engine.score_priority("Furniture", "Broken bench", "a bench is broken", 1, 0, False, 0)
    spark, lab, f = ai_engine.score_priority("Electrical", "Fan sparking", "unsafe, burning smell", 1, 0, False, 0)
    crowd, _, _ = ai_engine.score_priority("Furniture", "Broken bench", "a bench is broken", 25, 0, False, 0)
    reopened, _, _ = ai_engine.score_priority("Furniture", "Broken bench", "a bench is broken", 1, 0, False, 2)
    assert spark > 45 and spark > base
    assert crowd > base + 15 and reopened > base + 10
    assert any(n == "Urgency language" for n, _, _ in f)


# ------------------------------------------------------------- access control
def test_register_only_for_roster_students(app):
    c = Client(app)
    assert b"not found" in c.post("/register", dict(roll_no="ZZ", email="nobody@gmail.com", password="secret1")).data
    assert b"not found" in c.post("/register", dict(roll_no="WRONG", email="sam@x.edu", password="secret1")).data   # right email, wrong roll
    assert c.post("/register", dict(roll_no="T1", email="sam@x.edu", password="secret1")).status_code == 302
    assert b"Sam" in c.get("/dashboard").data                      # name comes from the roster


def test_hostel_only_for_roster_residents(app):
    day, res = register(app, "Sam"), register(app, "Res")
    r = day.get("/hostel")
    assert r.status_code == 302 and "/hostel/verify" in r.headers["Location"]
    assert day.get("/report/hostel").status_code == 302
    assert b"Locked" in day.get("/hostel/verify").data
    assert res.get("/hostel").status_code == 200


def test_roster_change_revokes_hostel_access(app, tmp_path):
    import campus_data
    res = register(app, "Res")
    assert res.get("/hostel").status_code == 200
    p = os.path.join(campus_data.DATA, "students.csv")
    txt = open(p).read().replace("T7,Res,res@x.edu,A,1", "T7,Res,res@x.edu,,")
    open(p, "w").write(txt); os.utime(p, (9e9, 9e9))
    assert res.get("/hostel").status_code == 302                    # access follows the CSV immediately


def test_locations_must_come_from_dataset_and_residents_limited_to_own_block(app):
    s, res = register(app, "Sam"), register(app, "Res")
    r = report(s, 1, building="Block X", place="Room 999")
    assert r.status_code == 200 and b"campus list" in r.data
    r = hreport(res, 2, building="Block B")                          # other hostel block
    assert r.status_code == 200 and b"campus list" in r.data
    assert hreport(res, 3).status_code == 302                         # own block OK
    assert hreport(res, 4, building="Common Areas", place="Mess Hall", title="Stale food served", desc="food was stale at dinner time").status_code == 302


def test_admin_pages_forbidden_for_students(app):
    s = register(app, "Sam")
    assert s.get("/admin").status_code in (302, 403)
    assert s.get("/admin/analytics").status_code == 403


def test_csrf_enforced(app):
    c = app.test_client()
    assert c.post("/login", data=dict(email="a@b.c", password="x")).status_code == 400


# ------------------------------------------------------------- evidence
def test_photo_mandatory_where_required(app):
    s = register(app, "Sam")
    r = report(s, 1, photo=False)
    assert r.status_code == 200 and b"mandatory" in r.data
    r = report(s, 1, cat="Wi-Fi / Network", title="Wi-Fi slow in lab", desc="internet is very slow in the lab", photo=False)
    assert r.status_code == 302  # photo optional for Wi-Fi


def test_blank_and_recycled_photos_rejected(app):
    s = register(app, "Sam"); t = register(app, "Tia")
    blank = io.BytesIO(); Image.new("RGB", (400, 300), (10, 10, 10)).save(blank, "PNG"); blank.seek(0)
    r = s.post("/report/classroom", dict(category="Furniture", title="Broken chair", description="the chair leg is broken badly",
                                         building="Block X", place="Room 5", photo=(blank, "b.png")), content_type="multipart/form-data")
    assert b"blank" in r.data
    assert report(s, 7, cat="Furniture", title="Broken chair", desc="the chair leg is broken badly", place="Room 5").status_code == 302
    r = report(t, 7, cat="Furniture", title="Cracked desk in hall", desc="the desk is cracked all over", place="Hall 9")
    assert b"already submitted" in r.data


# ------------------------------------------------------------- dedup & impact
def test_me_too_raises_priority_and_merge(app):
    a, b, c = register(app, "Amy"), register(app, "Bob"), register(app, "Cat")
    first = cid_from(report(a, 1))
    r = report(b, 2, title="Projector not turning on", desc="Projector does not turn on in room 301.")
    assert r.status_code == 200 and b"AI found similar" in r.data       # gate shows match, nothing created yet
    before = a.get(f"/complaint/{first}").data
    b.post(f"/complaint/{first}/support")
    r2 = report(c, 3, title="Projector not working in room 301", desc="Projector in room 301 not working, blank screen.", force=True)
    assert cid_from(r2) == first                                         # AI auto-merged into the parent
    page = a.get(f"/complaint/{first}").data.decode()
    assert "3 affected" in page and "Merged duplicate" in page


def test_different_room_not_flagged(app):
    a, b = register(app, "Amy"), register(app, "Bob")
    report(a, 1)
    r = report(b, 2, title="Projector not working in room 512", desc="Projector in room 512 not turning on.", place="Room 512")
    assert r.status_code == 302


# ------------------------------------------------------------- resolution & verification
def _setup_pending(app):
    a, b, adm = register(app, "Amy"), register(app, "Bob"), make_admin(app)
    cid = cid_from(report(a, 1))
    b.post(f"/complaint/{cid}/support")
    return a, b, adm, cid


def test_resolution_needs_distinct_after_photo(app):
    a, b, adm, cid = _setup_pending(app)
    assert b"after" in adm.post(f"/admin/complaint/{cid}/resolve", dict(note="fixed it"), follow_redirects=True).data.lower()
    # identical to before photo → rejected
    r = adm.post(f"/admin/complaint/{cid}/resolve", dict(note="fixed it", photo=(img(1), "x.png")),
                 content_type="multipart/form-data", follow_redirects=True)
    assert b"identical" in r.data
    r = adm.post(f"/admin/complaint/{cid}/resolve", dict(note="fixed it", photo=(img(99), "x.png")),
                 content_type="multipart/form-data", follow_redirects=True)
    assert b"Awaiting student verification" in r.data


def test_students_close_or_reopen(app):
    a, b, adm, cid = _setup_pending(app)
    adm.post(f"/admin/complaint/{cid}/resolve", dict(note="fixed it", photo=(img(99), "x.png")), content_type="multipart/form-data")
    assert b"Yes, it" in a.get(f"/complaint/{cid}").data                  # reporter sees vote buttons
    a.post(f"/complaint/{cid}/verify", dict(verdict="fixed"))
    assert b"Awaiting student verification" in a.get(f"/complaint/{cid}").data  # 1 of 2 – not yet a majority
    b.post(f"/complaint/{cid}/verify", dict(verdict="fixed"))
    assert b"Resolved &amp; verified" in a.get(f"/complaint/{cid}").data


def test_rejected_fix_reopens_with_higher_priority(app):
    a, adm = register(app, "Amy"), make_admin(app)
    cid = cid_from(report(a, 1))
    import sqlite3
    p0 = sqlite3.connect(app.config["DATABASE"]).execute("SELECT priority_score FROM complaints WHERE id=?", (cid,)).fetchone()[0]
    adm.post(f"/admin/complaint/{cid}/resolve", dict(note="fixed it", photo=(img(99), "x.png")), content_type="multipart/form-data")
    a.post(f"/complaint/{cid}/verify", dict(verdict="not_fixed", comment="still dead"))
    row = sqlite3.connect(app.config["DATABASE"]).execute("SELECT status, reopen_count, priority_score FROM complaints WHERE id=?", (cid,)).fetchone()
    assert row[0] == "open" and row[1] == 1 and row[2] > p0


def test_only_affected_students_can_verify(app):
    a, b, adm = register(app, "Amy"), register(app, "Bob"), make_admin(app)
    cid = cid_from(report(a, 1))
    adm.post(f"/admin/complaint/{cid}/resolve", dict(note="fixed it", photo=(img(99), "x.png")), content_type="multipart/form-data")
    b.post(f"/complaint/{cid}/verify", dict(verdict="fixed"))
    import sqlite3
    assert sqlite3.connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM verifications").fetchone()[0] == 0


def test_silence_closes_after_window(app):
    a, adm = register(app, "Amy"), make_admin(app)
    cid = cid_from(report(a, 1))
    adm.post(f"/admin/complaint/{cid}/resolve", dict(note="fixed it", photo=(img(99), "x.png")), content_type="multipart/form-data")
    import sqlite3
    d = sqlite3.connect(app.config["DATABASE"]); d.execute("UPDATE complaints SET verify_deadline='2000-01-01 00:00:00'"); d.commit()
    a.get("/dashboard")
    assert d.execute("SELECT status FROM complaints WHERE id=?", (cid,)).fetchone()[0] == "resolved"


def test_hostel_content_hidden_from_non_residents(app):
    r, s = register(app, "Res"), register(app, "Out")
    cid = cid_from(hreport(r, 1))
    assert s.get(f"/complaint/{cid}").status_code == 403
    assert r.get(f"/complaint/{cid}").status_code == 200


def test_uploads_require_login_and_valid_names(app):
    assert app.test_client().get("/uploads/" + "a" * 32 + ".jpg").status_code == 302
    s = register(app, "Sam")
    assert s.get("/uploads/../app.py").status_code == 404


# ------------------------------------------------------------- AI photo relevance
def _fake_relevance(monkeypatch, status, reason="Looks like a bedroom."):
    import relevance
    monkeypatch.setattr(relevance, "check", lambda *a, **k: (status, reason))


def test_irrelevant_photo_is_rejected(app, monkeypatch):
    _fake_relevance(monkeypatch, "irrelevant")
    s = register(app, "Sam")
    r = report(s, 1)
    assert r.status_code == 200 and b"doesn" in r.data and b"bedroom" in r.data
    import sqlite3
    assert sqlite3.connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM complaints").fetchone()[0] == 0
    assert not os.listdir(app.config["UPLOAD_DIR"])                  # rejected file is deleted, not kept


def test_relevant_photo_marked_verified_and_unchecked_when_ai_off(app, monkeypatch):
    s = register(app, "Sam")
    cid = cid_from(report(s, 1))                                      # no API key -> unchecked
    assert b"awaiting admin review" in s.get(f"/complaint/{cid}").data
    _fake_relevance(monkeypatch, "verified", "ok")
    cid2 = cid_from(report(s, 2, place="Room 512", title="Projector dead in 512", desc="projector in 512 is dead, nothing shows"))
    assert "AI-verified" in s.get(f"/complaint/{cid2}").data.decode()


def test_admin_can_flag_photo_and_student_replaces_it(app, monkeypatch):
    s, adm = register(app, "Sam"), make_admin(app)
    cid = cid_from(report(s, 1))
    adm.post(f"/admin/complaint/{cid}/flag-photo", dict(reason="This is a selfie"))
    page = s.get(f"/complaint/{cid}").data
    assert b"flagged" in page and b"selfie" in page and b"Upload a relevant photo" in page
    _fake_relevance(monkeypatch, "irrelevant")
    r = s.post(f"/complaint/{cid}/photo", dict(photo=(img(50), "n.png")), content_type="multipart/form-data", follow_redirects=True)
    assert b"still not relevant" in r.data
    _fake_relevance(monkeypatch, "verified", "ok")
    s.post(f"/complaint/{cid}/photo", dict(photo=(img(51), "n.png")), content_type="multipart/form-data")
    assert b"AI-verified" in s.get(f"/complaint/{cid}").data


def test_after_photo_must_be_relevant_too(app, monkeypatch):
    s, adm = register(app, "Sam"), make_admin(app)
    cid = cid_from(report(s, 1))
    _fake_relevance(monkeypatch, "irrelevant", "Not the projector.")
    r = adm.post(f"/admin/complaint/{cid}/resolve", dict(note="fixed it", photo=(img(99), "x.png")),
                 content_type="multipart/form-data", follow_redirects=True)
    assert b"doesn" in r.data and b"Awaiting student verification" not in r.data


def test_cannot_attach_someone_elses_uploaded_file(app):
    a, b = register(app, "Amy"), register(app, "Bob")
    cid = cid_from(report(a, 1))
    import sqlite3
    victim = sqlite3.connect(app.config["DATABASE"]).execute("SELECT photo FROM complaints WHERE id=?", (cid,)).fetchone()[0]
    r = b.post("/report/classroom", dict(category="Furniture", title="Broken chair here", description="the chair is broken badly",
                                         building="Block X", place="Room 5", photo_file=victim, photo_hash="00"))
    assert b"mandatory" in r.data                                     # carried filename ignored -> still needs its own photo


def test_vision_response_parsing_and_failure_fallback(monkeypatch, tmp_path):
    import relevance, json, io as _io
    assert relevance.parse('Sure: {"relevant": false, "reason": "A bedroom."}') == (False, "A bedroom.")
    p = tmp_path / "a.jpg"; img(1).seek(0); p.write_bytes(img(1).read())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    class R(_io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    reply = {"content": [{"type": "text", "text": '{"relevant": true, "reason": "Shows a projector."}'}]}
    monkeypatch.setattr(relevance.urllib.request, "urlopen", lambda *a, **k: R(json.dumps(reply).encode()))
    assert relevance.check(str(p), "classroom", "Projector / AV", "t", "d", "l")[0] == "verified"
    def boom(*a, **k): raise OSError("offline")
    monkeypatch.setattr(relevance.urllib.request, "urlopen", boom)
    assert relevance.check(str(p), "classroom", "Projector / AV", "t", "d", "l")[0] == "unchecked"  # fails open, never blocks
