import json
import os
import re
import secrets
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import campus_data
import relevance
import services as S
import vision
from db import close_db, get_db, init_db

BASE = os.path.dirname(os.path.abspath(__file__))


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DATABASE=os.path.join(BASE, "instance", "campusloop.db"),
        UPLOAD_DIR=os.path.join(BASE, "instance", "uploads"),
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    )
    if config:
        app.config.update(config)
    init_db(app.config["DATABASE"])
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    app.teardown_appcontext(close_db)

    # ---------------------------------------------------------- helpers
    @app.before_request
    def load_user():
        uid = session.get("uid")
        db = get_db()
        g.user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None
        if g.user and g.user["role"] == "student":  # hostel access always follows the roster CSV
            row = campus_data.find_student(g.user["email"])
            want = ("verified", row["hostel_block"], row["hostel_room"]) if row and row["hostel_block"] else ("none", None, None)
            if want != (g.user["hostel_status"], g.user["hostel_block"], g.user["room_no"]):
                db.execute("UPDATE users SET hostel_status=?, hostel_block=?, room_no=? WHERE id=?", (*want, uid))
                db.commit()
                g.user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if request.method == "POST":
            tok = request.form.get("csrf") or request.headers.get("X-CSRF")
            if not tok or tok != session.get("csrf"):
                abort(400, "Invalid or missing CSRF token")

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        pending = 0
        if g.get("user"):
            db = get_db()
            if g.user["role"] != "admin":
                pending = len(to_verify(db, g.user))
        return dict(csrf=session["csrf"], me=g.get("user"), STATUS_LABEL=S.STATUS_LABEL,
                    nav_pending=pending, can_access=S.can_access_module)

    @app.template_filter("dt")
    def dt_filter(s):
        d = S.parse(s)
        return d.strftime("%d %b %Y, %H:%M") if d else ""

    @app.template_filter("ago")
    def ago(s):
        d = S.parse(s)
        if not d:
            return ""
        sec = (S.now() - d).total_seconds()
        for lim, div, unit in ((3600, 60, "min"), (86400, 3600, "h"), (10**9, 86400, "d")):
            if sec < lim:
                return f"{max(1, int(sec // div))} {unit} ago"

    @app.template_filter("factors")
    def factors(s):
        return json.loads(s) if s else []

    def login_required(f):
        @wraps(f)
        def w(*a, **k):
            if not g.user:
                flash("Please sign in first.", "warn")
                return redirect(url_for("login", next=request.path))
            return f(*a, **k)
        return w

    def admin_required(f):
        @wraps(f)
        def w(*a, **k):
            if not g.user or g.user["role"] != "admin":
                abort(403)
            return f(*a, **k)
        return w

    def module_guard(module):
        if module not in S.CATEGORIES:
            abort(404)
        if not S.can_access_module(g.user, module):
            if module == "hostel":
                flash("The Hostel module is only for students listed as hostel residents in the college roster.", "warn")
                return redirect(url_for("hostel_verify"))
            abort(403)

    def to_verify(db, user):
        out = []
        for r in db.execute("SELECT id FROM complaints WHERE status='pending_verification' AND related_to IS NULL"):
            if user["id"] in S.affected_users(db, r["id"]):
                res = S.latest_resolution(db, r["id"])
                voted = db.execute("SELECT 1 FROM verifications WHERE resolution_id=? AND user_id=?",
                                   (res["id"], user["id"])).fetchone()
                if not voted:
                    out.append(r["id"])
        return out

    # ---------------------------------------------------------- public / auth
    @app.route("/")
    def home():
        db = get_db()
        stats = dict(
            total=db.execute("SELECT COUNT(*) FROM complaints").fetchone()[0],
            resolved=db.execute("SELECT COUNT(*) FROM complaints WHERE status='resolved'").fetchone()[0],
            merged=db.execute("SELECT COUNT(*) FROM complaints WHERE related_to IS NOT NULL").fetchone()[0],
            caught=db.execute("SELECT COUNT(*) FROM verifications WHERE verdict='not_fixed'").fetchone()[0])
        return render_template("home.html", stats=stats)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            roll, email, pw = request.form["roll_no"].strip(), request.form["email"].strip().lower(), request.form["password"]
            row = campus_data.find_student(email, roll)
            db = get_db()
            if not row:
                flash("Roll number and email were not found in the college student list. Use your registered college email.", "bad")
            elif len(pw) < 6:
                flash("Password must be 6+ characters.", "bad")
            elif db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                flash("That email is already registered. Sign in instead.", "bad")
            else:
                hostel = bool(row["hostel_block"])
                cur = db.execute("""INSERT INTO users(name,email,password_hash,hostel_status,hostel_block,room_no,created_at)
                                    VALUES(?,?,?,?,?,?,?)""",
                                 (row["name"], email, generate_password_hash(pw), "verified" if hostel else "none",
                                  row["hostel_block"] or None, row["hostel_room"] or None, S.ts()))
                db.commit()
                session.clear()
                session["uid"] = cur.lastrowid
                flash(f"Welcome {row['name']}! " + ("Classroom and Hostel (Block %s) are unlocked." % row["hostel_block"] if hostel
                                                     else "Classroom module unlocked."), "ok")
                return redirect(url_for("dashboard"))
        return render_template("auth.html", mode="register")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            u = get_db().execute("SELECT * FROM users WHERE email=?", (request.form["email"].strip().lower(),)).fetchone()
            if u and check_password_hash(u["password_hash"], request.form["password"]):
                session.clear()
                session["uid"] = u["id"]
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
            flash("Wrong email or password.", "bad")
        return render_template("auth.html", mode="login")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("home"))

    @app.route("/uploads/<path:name>")
    @login_required
    def uploads(name):
        if not re.fullmatch(r"[0-9a-f]{32}\.jpg", name):
            abort(404)
        return send_from_directory(app.config["UPLOAD_DIR"], name)

    # ---------------------------------------------------------- student
    @app.route("/dashboard")
    @login_required
    def dashboard():
        if g.user["role"] == "admin":
            return redirect(url_for("admin"))
        db = get_db()
        S.sweep(db)
        mine = db.execute("""SELECT c.*, (SELECT COUNT(*) FROM supporters s WHERE s.complaint_id=c.id) sup FROM complaints c
                             WHERE reporter_id=? OR id IN (SELECT complaint_id FROM supporters WHERE user_id=?)
                             ORDER BY c.created_at DESC""", (g.user["id"], g.user["id"])).fetchall()
        verify_ids = to_verify(db, g.user)
        need = [db.execute("SELECT * FROM complaints WHERE id=?", (i,)).fetchone() for i in verify_ids]
        return render_template("dashboard.html", mine=mine, need=need)

    @app.route("/hostel/verify")
    @login_required
    def hostel_verify():
        return render_template("hostel_verify.html", u=g.user)

    @app.route("/<module>")
    @login_required
    def module_list(module):
        if module not in S.CATEGORIES:
            abort(404)
        r = module_guard(module)
        if r:
            return r
        db = get_db()
        S.sweep(db)
        S.recompute_open(db)
        db.commit()
        status, cat = request.args.get("status", ""), request.args.get("category", "")
        q = "SELECT c.*, u.name reporter FROM complaints c JOIN users u ON u.id=c.reporter_id WHERE module=? AND related_to IS NULL"
        args = [module]
        if status:
            q += " AND status=?"
            args.append(status)
        else:
            q += " AND status!='resolved'" if request.args.get("show") != "all" else ""
        if cat:
            q += " AND category=?"
            args.append(cat)
        q += " ORDER BY (status='resolved'), priority_score DESC, created_at DESC"
        rows = db.execute(q, args).fetchall()
        impact = {r["id"]: len(S.affected_users(db, r["id"])) for r in rows}
        return render_template("list.html", module=module, rows=rows, impact=impact, cats=S.CATEGORIES[module],
                               status=status, cat=cat, show=request.args.get("show", ""))

    # ---------------------------------------------------------- reporting
    def similar_payload(matches):
        return [dict(id=c["id"], title=c["title"], location=c["location"], category=c["category"], score=s,
                     reasons=r, status=c["status"]) for c, s, r in matches]

    @app.route("/api/similar")
    @login_required
    def api_similar():
        module = request.args.get("module", "")
        if module not in S.CATEGORIES or not S.can_access_module(g.user, module):
            abort(403)
        text = request.args.get("text", "")
        if len(text.strip()) < 8:
            return jsonify([])
        m = S.similar_open(get_db(), module, request.args.get("category", ""), text, "", request.args.get("location", ""))
        return jsonify(similar_payload(m))

    @app.route("/report/<module>", methods=["GET", "POST"])
    @login_required
    def report(module):
        if module not in S.CATEGORIES:
            abort(404)
        r = module_guard(module)
        if r:
            return r
        if g.user["role"] == "admin":
            flash("Admins resolve complaints; use a student account to report.", "warn")
            return redirect(url_for("admin"))
        cats = S.CATEGORIES[module]
        db = get_db()
        stu = campus_data.find_student(g.user["email"])
        locs = campus_data.locations(module, stu if module == "hostel" else None)
        form = dict(category=request.form.get("category", ""), title=request.form.get("title", "").strip(),
                    description=request.form.get("description", "").strip(),
                    building=request.form.get("building", ""), place=request.form.get("place", ""))
        if module == "hostel" and request.method == "GET" and stu:
            form["building"] = f"Block {stu['hostel_block']}"
        matches, photo_name, photo_hash = [], request.form.get("photo_file", ""), request.form.get("photo_hash", "")
        chk = dict(session.get("pchk", {}))
        pnote = ""

        def page(**kw):
            return render_template("report.html", module=module, cats=cats, form=form, locs=locs,
                                   matches=kw.get("matches", []), photo_name=photo_name, photo_hash=photo_hash,
                                   ai_on=relevance.enabled())

        if request.method == "POST":
            err = None
            if form["category"] not in cats:
                err = "Choose a category."
            elif not campus_data.valid_location(module, form["building"], form["place"], stu if module == "hostel" else None):
                err = "Pick the building and place from the campus list."
            elif len(form["title"]) < 5 or len(form["description"]) < 10:
                err = "Please give a short title and a description (10+ characters)."
            if not err:
                location = campus_data.compose(form["building"], form["place"], g.user["room_no"])
                need_photo = cats[form["category"]]
                pstatus = "none"
                if photo_name:  # photo carried over from the duplicate-check round-trip: must be one WE checked for this session
                    path = os.path.join(app.config["UPLOAD_DIR"], photo_name)
                    if photo_name in chk and re.fullmatch(r"[0-9a-f]{32}\.jpg", photo_name) and os.path.exists(path):
                        pstatus, pnote = chk[photo_name]
                    else:
                        photo_name, photo_hash = "", ""
                f = request.files.get("photo")
                if f and f.filename:
                    known = [x[0] for x in db.execute("SELECT photo_hash FROM complaints WHERE photo_hash IS NOT NULL")]
                    try:
                        photo_name, photo_hash = vision.validate_and_save(f, app.config["UPLOAD_DIR"], known)
                        path = os.path.join(app.config["UPLOAD_DIR"], photo_name)
                        pstatus, pnote = relevance.check(path, module, form["category"], form["title"], form["description"], location)
                        if pstatus == "irrelevant":
                            os.remove(path)
                            photo_name, photo_hash = "", ""
                            err = f"AI photo check: this photo doesn't look like evidence of this complaint. {pnote} Please take a photo of the actual problem."
                        else:
                            chk[photo_name] = [pstatus, pnote]
                            session["pchk"] = dict(list(chk.items())[-6:])
                    except vision.PhotoError as e:
                        err = str(e)
                if not err and need_photo and not photo_name:
                    err = f"A photo is mandatory for “{form['category']}” complaints — please attach clear evidence."
            if err:
                flash(err, "bad")
            else:
                matches = S.similar_open(db, module, form["category"], form["title"], form["description"], location)
                if matches and request.form.get("force") != "1":
                    return page(matches=matches)
                cid = S.create_complaint(db, g.user, module, form["category"], form["title"], form["description"],
                                         location, photo_name or None, photo_hash or None,
                                         force=request.form.get("force") == "1", photo_status=pstatus, photo_note=pnote)
                flash("Complaint submitted. AI has analysed and prioritised it.", "ok")
                return redirect(url_for("complaint", cid=S.root_id(db, cid)))
        return page()

    # ---------------------------------------------------------- complaint detail & actions
    @app.route("/complaint/<int:cid>")
    @login_required
    def complaint(cid):
        db = get_db()
        S.sweep(db)
        c = db.execute("SELECT c.*, u.name reporter FROM complaints c JOIN users u ON u.id=c.reporter_id WHERE c.id=?", (cid,)).fetchone()
        if not c:
            abort(404)
        if not S.can_access_module(g.user, c["module"]):
            abort(403)
        if c["related_to"]:
            flash(f"This report was merged into #{c['related_to']} by AI duplicate detection.", "info")
            return redirect(url_for("complaint", cid=c["related_to"]))
        S.recompute_priority(db, cid)
        db.commit()
        c = db.execute("SELECT c.*, u.name reporter FROM complaints c JOIN users u ON u.id=c.reporter_id WHERE c.id=?", (cid,)).fetchone()
        affected = S.affected_users(db, cid)
        children = db.execute("SELECT c.*, u.name reporter FROM complaints c JOIN users u ON u.id=c.reporter_id WHERE related_to=?", (cid,)).fetchall()
        res_list = db.execute("SELECT r.*, u.name admin FROM resolutions r JOIN users u ON u.id=r.admin_id WHERE complaint_id=? ORDER BY r.id DESC", (cid,)).fetchall()
        latest = res_list[0] if res_list else None
        votes = db.execute("""SELECT v.*, u.name FROM verifications v JOIN users u ON u.id=v.user_id
                              WHERE resolution_id=? ORDER BY v.id""", (latest["id"],)).fetchall() if latest else []
        my_vote = next((v for v in votes if v["user_id"] == g.user["id"]), None)
        fixed, bad = S.vote_counts(db, cid)
        log = db.execute("SELECT * FROM activity WHERE complaint_id IN (%s) ORDER BY id DESC" %
                         ",".join("?" * len(S.family_ids(db, cid))), S.family_ids(db, cid)).fetchall()
        return render_template("complaint.html", c=c, n=len(affected), is_affected=g.user["id"] in affected, children=children,
                               res_list=res_list, latest=latest, votes=votes, my_vote=my_vote, fixed=fixed, bad=bad,
                               log=log, window=S.VERIFY_WINDOW_HOURS)

    @app.post("/complaint/<int:cid>/support")
    @login_required
    def support(cid):
        c = get_db().execute("SELECT module FROM complaints WHERE id=?", (cid,)).fetchone()
        if not c or not S.can_access_module(g.user, c["module"]) or g.user["role"] == "admin":
            abort(403)
        ok = S.add_support(get_db(), cid, g.user)
        flash("Added — your voice raised this issue's priority." if ok else "You're already counted on this issue.", "ok" if ok else "info")
        return redirect(url_for("complaint", cid=S.root_id(get_db(), cid)))

    @app.post("/complaint/<int:cid>/verify")
    @login_required
    def verify(cid):
        verdict = request.form.get("verdict")
        if verdict not in ("fixed", "not_fixed"):
            abort(400)
        ok = S.verify(get_db(), cid, g.user, verdict, request.form.get("comment", "").strip()[:300])
        flash("Thanks — your verification was recorded." if ok else "You can't verify this right now.", "ok" if ok else "bad")
        return redirect(url_for("complaint", cid=cid))

    # ---------------------------------------------------------- admin
    @app.route("/admin")
    @admin_required
    def admin():
        db = get_db()
        S.sweep(db)
        S.recompute_open(db)
        db.commit()
        module = request.args.get("module", "")
        q = "SELECT c.*, u.name reporter FROM complaints c JOIN users u ON u.id=c.reporter_id WHERE related_to IS NULL AND status!='resolved'"
        args = []
        if module in S.CATEGORIES:
            q += " AND module=?"
            args.append(module)
        rows = db.execute(q + " ORDER BY priority_score DESC", args).fetchall()
        impact = {r["id"]: len(S.affected_users(db, r["id"])) for r in rows}
        dupes = {r["id"]: db.execute("SELECT COUNT(*) FROM complaints WHERE related_to=?", (r["id"],)).fetchone()[0] for r in rows}
        counts = {k: db.execute("SELECT COUNT(*) FROM complaints WHERE status=? AND related_to IS NULL", (k,)).fetchone()[0] for k in S.STATUS_LABEL}
        return render_template("admin.html", rows=rows, impact=impact, dupes=dupes, counts=counts, module=module)

    @app.post("/admin/complaint/<int:cid>/progress")
    @admin_required
    def admin_progress(cid):
        S.mark_in_progress(get_db(), cid, g.user, request.form.get("note", "").strip()[:200])
        flash("Marked as in progress.", "ok")
        return redirect(url_for("complaint", cid=cid))

    @app.post("/admin/complaint/<int:cid>/resolve")
    @admin_required
    def admin_resolve(cid):
        db = get_db()
        c = db.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
        note, f = request.form.get("note", "").strip(), request.files.get("photo")
        if not c or c["status"] == "resolved" or c["related_to"]:
            abort(400)
        if len(note) < 5 or not f or not f.filename:
            flash("Resolution needs a note AND an 'after' photo as proof.", "bad")
            return redirect(url_for("complaint", cid=cid))
        try:
            name, h = vision.validate_and_save(f, app.config["UPLOAD_DIR"],
                                               [x[0] for x in db.execute("SELECT photo_hash FROM resolutions")],
                                               reject_if_near=c["photo_hash"])
            st, note = relevance.check(os.path.join(app.config["UPLOAD_DIR"], name), c["module"], c["category"],
                                       c["title"], c["description"], c["location"], kind="after")
            if st == "irrelevant":
                os.remove(os.path.join(app.config["UPLOAD_DIR"], name))
                flash(f"AI photo check: the 'after' photo doesn't show the fixed item. {note}", "bad")
                return redirect(url_for("complaint", cid=cid))
        except vision.PhotoError as e:
            flash(str(e), "bad")
            return redirect(url_for("complaint", cid=cid))
        S.submit_resolution(db, cid, g.user, note, name, h)
        flash("Resolution proof uploaded. Students will now verify it.", "ok")
        return redirect(url_for("complaint", cid=cid))

    @app.post("/admin/complaint/<int:cid>/unlink")
    @admin_required
    def admin_unlink(cid):
        S.unlink(get_db(), cid, g.user)
        flash("Report split from its parent.", "ok")
        return redirect(url_for("complaint", cid=cid))

    @app.post("/admin/complaint/<int:cid>/flag-photo")
    @admin_required
    def admin_flag_photo(cid):
        reason = request.form.get("reason", "").strip()[:200] or "Photo does not show the reported problem."
        S.flag_photo(get_db(), cid, g.user, reason)
        flash("Photo flagged. The reporter has been asked to upload a relevant photo.", "ok")
        return redirect(url_for("complaint", cid=cid))

    @app.post("/complaint/<int:cid>/photo")
    @login_required
    def replace_photo(cid):
        db = get_db()
        c = db.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
        if not c or c["reporter_id"] != g.user["id"] or c["photo_status"] != "flagged":
            abort(403)
        f = request.files.get("photo")
        if not f or not f.filename:
            flash("Choose a new photo.", "bad")
            return redirect(url_for("complaint", cid=cid))
        try:
            known = [x[0] for x in db.execute("SELECT photo_hash FROM complaints WHERE photo_hash IS NOT NULL")]
            name, h = vision.validate_and_save(f, app.config["UPLOAD_DIR"], known)
            path = os.path.join(app.config["UPLOAD_DIR"], name)
            st, note = relevance.check(path, c["module"], c["category"], c["title"], c["description"], c["location"])
            if st == "irrelevant":
                os.remove(path)
                flash(f"AI photo check: still not relevant. {note}", "bad")
                return redirect(url_for("complaint", cid=cid))
        except vision.PhotoError as e:
            flash(str(e), "bad")
            return redirect(url_for("complaint", cid=cid))
        S.replace_photo(db, cid, g.user, name, h, st, note)
        flash("New photo saved.", "ok")
        return redirect(url_for("complaint", cid=cid))

    @app.route("/admin/analytics")
    @admin_required
    def analytics():
        db = get_db()
        S.sweep(db)
        q = lambda sql, *a: db.execute(sql, a).fetchall()
        by_cat = q("SELECT module, category, COUNT(*) n FROM complaints GROUP BY module, category ORDER BY n DESC")
        hot = q("""SELECT module, category, lower(location) loc, COUNT(*) n, SUM(status!='resolved') open_n FROM complaints
                   WHERE created_at>=? GROUP BY module, category, lower(location) HAVING n>=2 ORDER BY n DESC LIMIT 10""",
                S.ts(S.now() - S.timedelta(days=60)))
        avg = db.execute("SELECT AVG((julianday(resolved_at)-julianday(created_at))*24) FROM complaints WHERE resolved_at IS NOT NULL").fetchone()[0]
        fixed = db.execute("SELECT COUNT(*) FROM verifications WHERE verdict='fixed'").fetchone()[0]
        bad = db.execute("SELECT COUNT(*) FROM verifications WHERE verdict='not_fixed'").fetchone()[0]
        merged = db.execute("SELECT COUNT(*) FROM complaints WHERE related_to IS NOT NULL").fetchone()[0]
        recurring = db.execute("SELECT COUNT(*) FROM complaints WHERE recurring=1").fetchone()[0]
        reopened = db.execute("SELECT COUNT(*) FROM complaints WHERE reopen_count>0").fetchone()[0]
        return render_template("analytics.html", by_cat=by_cat, hot=hot, avg=avg, fixed=fixed, bad=bad, merged=merged,
                               recurring=recurring, reopened=reopened)

    @app.errorhandler(403)
    def e403(e):
        return render_template("error.html", code=403, msg="You don't have access to this page."), 403

    @app.errorhandler(404)
    def e404(e):
        return render_template("error.html", code=404, msg="That page doesn't exist."), 404

    @app.errorhandler(400)
    def e400(e):
        return render_template("error.html", code=400, msg=getattr(e, "description", "Bad request")), 400

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
