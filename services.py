"""Business logic: complaints, merging, priority, resolution & student verification."""
import json
from datetime import datetime, timedelta, timezone

import ai_engine

VERIFY_WINDOW_HOURS = 72

CATEGORIES = {
    "classroom": {  # name: photo_required
        "Projector / AV": True, "Electrical": True, "Furniture": True, "Cleanliness": True,
        "Cooling / Ventilation": True, "Safety hazard": True, "Wi-Fi / Network": False, "Other": False},
    "hostel": {
        "Water supply": False, "Plumbing / Leakage": True, "Electrical": True, "Mess / Food quality": True,
        "Cleanliness / Hygiene": True, "Furniture / Room": True, "Pest control": True, "Security": False,
        "Wi-Fi / Network": False, "Noise": False, "Safety hazard": True},
}
STATUS_LABEL = {"open": "Open", "in_progress": "In progress", "pending_verification": "Awaiting student verification",
                "resolved": "Resolved & verified"}


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def ts(dt=None):
    return (dt or now()).strftime("%Y-%m-%d %H:%M:%S")


def parse(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None


def log(db, cid, text):
    db.execute("INSERT INTO activity(complaint_id,text,created_at) VALUES(?,?,?)", (cid, text, ts()))


# ------------------------------------------------------------ access control
def can_access_module(user, module):
    if not user:
        return False
    if user["role"] == "admin" or module == "classroom":
        return True
    return module == "hostel" and user["hostel_status"] == "verified"


# ------------------------------------------------------------ impact
def family_ids(db, cid):
    ids = [cid] + [r["id"] for r in db.execute("SELECT id FROM complaints WHERE related_to=?", (cid,))]
    return ids


def affected_users(db, cid):
    ids = family_ids(db, cid)
    q = ",".join("?" * len(ids))
    users = {r[0] for r in db.execute(f"SELECT reporter_id FROM complaints WHERE id IN ({q})", ids)}
    users |= {r[0] for r in db.execute(f"SELECT user_id FROM supporters WHERE complaint_id IN ({q})", ids)}
    return users


def recompute_priority(db, cid):
    c = db.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
    impact = len(affected_users(db, cid))
    age = (now() - parse(c["created_at"])).total_seconds() / 86400
    # recurring = a similar problem at this place was resolved in the last 60 days
    since = ts(now() - timedelta(days=60))
    recurring = 0
    for r in db.execute("""SELECT * FROM complaints WHERE module=? AND category=? AND status='resolved'
                           AND resolved_at>=? AND id!=?""", (c["module"], c["category"], since, cid)):
        if ai_engine.loc_similarity(c["location"], r["location"]) >= 0.5:
            recurring = 1
            break
    score, label, factors = ai_engine.score_priority(c["category"], c["title"], c["description"], impact,
                                                      age, recurring, c["reopen_count"])
    db.execute("UPDATE complaints SET priority_score=?, priority_label=?, priority_why=?, recurring=? WHERE id=?",
               (score, label, json.dumps(factors), recurring, cid))
    return score


def recompute_open(db):
    for r in db.execute("SELECT id FROM complaints WHERE status!='resolved' AND related_to IS NULL").fetchall():
        recompute_priority(db, r["id"])


# ------------------------------------------------------------ similarity
def similar_open(db, module, category, title, description, location, limit=3):
    cands = [dict(r) for r in db.execute(
        "SELECT * FROM complaints WHERE module=? AND status!='resolved' AND related_to IS NULL", (module,))]
    return ai_engine.find_similar(dict(category=category, title=title, description=description, location=location),
                                  cands, limit=limit)


# ------------------------------------------------------------ create / support
AUTO_LINK_SCORE = 0.70


def create_complaint(db, user, module, category, title, description, location, photo, photo_hash, force=False,
                     photo_status=None, photo_note=None):
    matches = similar_open(db, module, category, title, description, location)
    related, link_score = None, None
    if force and matches and matches[0][1] >= AUTO_LINK_SCORE:
        related, link_score = matches[0][0]["id"], matches[0][1]
    t = ts()
    cur = db.execute("""INSERT INTO complaints(module,category,title,description,location,photo,photo_hash,photo_status,photo_note,
                        reporter_id,related_to,link_score,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (module, category, title, description, location, photo, photo_hash,
                      (photo_status or "unchecked") if photo else "none", photo_note, user["id"],
                      related, link_score, t, t))
    cid = cur.lastrowid
    log(db, cid, f"{user['name']} reported the issue.")
    if related:
        log(db, cid, f"AI linked this report to #{related} (similarity {link_score}). Impact merged.")
        log(db, related, f"AI merged duplicate report #{cid} — more students are affected.")
        recompute_priority(db, related)
    else:
        recompute_priority(db, cid)
    db.commit()
    return cid


def root_id(db, cid):
    r = db.execute("SELECT related_to FROM complaints WHERE id=?", (cid,)).fetchone()
    return r["related_to"] or cid


def add_support(db, cid, user):
    cid = root_id(db, cid)
    if user["id"] in affected_users(db, cid):
        return False
    db.execute("INSERT INTO supporters VALUES(?,?,?)", (cid, user["id"], ts()))
    log(db, cid, f"{user['name']} confirmed they face the same problem (+1 impact).")
    recompute_priority(db, cid)
    db.commit()
    return True


# ------------------------------------------------------------ admin actions
def mark_in_progress(db, cid, admin, note=""):
    db.execute("UPDATE complaints SET status='in_progress', updated_at=? WHERE id=?", (ts(), cid))
    mirror(db, cid)
    log(db, cid, f"Admin {admin['name']} started work." + (f" Note: {note}" if note else ""))
    db.commit()


def mirror(db, cid):
    """Duplicates always follow the status of their parent complaint."""
    p = db.execute("SELECT status, verify_deadline, resolved_at, reopen_count FROM complaints WHERE id=?", (cid,)).fetchone()
    db.execute("UPDATE complaints SET status=?, verify_deadline=?, resolved_at=?, reopen_count=?, updated_at=? WHERE related_to=?",
               (p["status"], p["verify_deadline"], p["resolved_at"], p["reopen_count"], ts(), cid))


def submit_resolution(db, cid, admin, note, photo, photo_hash):
    deadline = ts(now() + timedelta(hours=VERIFY_WINDOW_HOURS))
    db.execute("INSERT INTO resolutions(complaint_id,admin_id,note,photo,photo_hash,created_at) VALUES(?,?,?,?,?,?)",
               (cid, admin["id"], note, photo, photo_hash, ts()))
    db.execute("UPDATE complaints SET status='pending_verification', verify_deadline=?, updated_at=? WHERE id=?",
               (deadline, ts(), cid))
    mirror(db, cid)
    log(db, cid, f"Admin {admin['name']} uploaded resolution proof. Affected students have {VERIFY_WINDOW_HOURS}h to verify.")
    db.commit()


def latest_resolution(db, cid):
    return db.execute("SELECT * FROM resolutions WHERE complaint_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()


def vote_counts(db, cid):
    res = latest_resolution(db, cid)
    if not res:
        return 0, 0
    rows = db.execute("SELECT verdict, COUNT(*) n FROM verifications WHERE resolution_id=? GROUP BY verdict", (res["id"],))
    d = {r["verdict"]: r["n"] for r in rows}
    return d.get("fixed", 0), d.get("not_fixed", 0)


def verify(db, cid, user, verdict, comment=""):
    c = db.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
    if c["status"] != "pending_verification" or user["id"] not in affected_users(db, cid):
        return False
    res = latest_resolution(db, cid)
    try:
        db.execute("INSERT INTO verifications(complaint_id,resolution_id,user_id,verdict,comment,created_at) VALUES(?,?,?,?,?,?)",
                   (cid, res["id"], user["id"], verdict, comment, ts()))
    except Exception:
        return False
    word = "confirmed the fix" if verdict == "fixed" else "reported it is NOT fixed"
    log(db, cid, f"{user['name']} {word}." + (f" “{comment}”" if comment else ""))
    evaluate(db, cid)
    db.commit()
    return True


def evaluate(db, cid, final=False):
    """Decide outcome of student verification. Majority rules; silence after the window counts as consent."""
    n = len(affected_users(db, cid))
    fixed, bad = vote_counts(db, cid)
    outcome = None
    if bad > n / 2 or (n == 1 and bad == 1):
        outcome = "reopen"
    elif fixed > n / 2 or (n == 1 and fixed == 1):
        outcome = "close"
    elif final:
        outcome = "reopen" if bad and bad >= fixed else "close"
    if outcome == "close":
        db.execute("UPDATE complaints SET status='resolved', resolved_at=?, updated_at=? WHERE id=?", (ts(), ts(), cid))
        log(db, cid, "✔ Resolution verified by students. Complaint closed.")
    elif outcome == "reopen":
        db.execute("UPDATE complaints SET status='open', reopen_count=reopen_count+1, verify_deadline=NULL, updated_at=? WHERE id=?",
                   (ts(), cid))
        log(db, cid, "✖ Students rejected the resolution. Complaint re-opened with higher priority.")
    if outcome:
        mirror(db, cid)
        recompute_priority(db, cid)
    return outcome


def sweep(db):
    """Auto-finalise verification windows that have expired."""
    rows = db.execute("SELECT id FROM complaints WHERE status='pending_verification' AND related_to IS NULL AND verify_deadline<=?",
                      (ts(),)).fetchall()
    for r in rows:
        evaluate(db, r["id"], final=True)
    if rows:
        db.commit()


def unlink(db, cid, admin):
    c = db.execute("SELECT related_to FROM complaints WHERE id=?", (cid,)).fetchone()
    if not c or not c["related_to"]:
        return
    parent = c["related_to"]
    db.execute("UPDATE complaints SET related_to=NULL, link_score=NULL WHERE id=?", (cid,))
    log(db, cid, f"Admin {admin['name']} split this report from #{parent} (AI link overruled).")
    log(db, parent, f"Admin split report #{cid} (not a duplicate).")
    recompute_priority(db, cid)
    recompute_priority(db, parent)
    db.commit()


def flag_photo(db, cid, admin, reason):
    db.execute("UPDATE complaints SET photo_status='flagged', photo_note=?, updated_at=? WHERE id=?", (reason, ts(), cid))
    log(db, cid, f"Admin {admin['name']} flagged the photo as not relevant: {reason}")
    db.commit()


def replace_photo(db, cid, user, photo, photo_hash, status, note):
    db.execute("UPDATE complaints SET photo=?, photo_hash=?, photo_status=?, photo_note=?, updated_at=? WHERE id=?",
               (photo, photo_hash, status, note, ts(), cid))
    log(db, cid, f"{user['name']} replaced the evidence photo.")
    db.commit()
