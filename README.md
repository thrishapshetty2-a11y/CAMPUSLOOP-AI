<<<<<<< HEAD
# CampusLoop AI — Intelligent Campus & Hostel Problem Resolution Platform
**Theme: Build for Campus** · Flask + SQLite + Pillow · runs offline, no API keys

> Report with photo evidence → AI merges duplicates & ranks by urgency and collective impact → admin fixes with
> proof → **affected students verify** the fix → closed (or re-opened at higher priority).

## Run it (2 minutes)
```bash
pip install -r requirements.txt
python seed.py          # demo data (resets instance/campusloop.db)
python app.py           # http://localhost:5000
python -m pytest -q     # 25 tests
```
| Role | Login |
|---|---|
| Admin | `admin@campus.edu` / `admin123` |
| Hostel resident | `aarav@campus.edu` / `student123` (also diya, isha, rohan) |
| Day scholar (no hostel access) | `meera@campus.edu` / `student123` (also vikram, sana, neha, arjun) |
| Try **registering** | roll `CS21010` + `priya@campus.edu` (hostel Block B) — anything not in the roster is refused |

## Your campus data (the "dataset") — edit these two CSV files
| File | What it controls |
|---|---|
| `data/students.csv` `roll_no,name,email,hostel_block,hostel_room` | **Who can register** (roll no + email must match). **Who gets Hostel access** (a hostel block listed = resident; blank = day scholar). Re-read automatically — remove a row's hostel block and that student loses hostel access on their next click. |
| `data/campus.csv` `module,building,place` | **Every valid location.** Students pick Building → Place from dropdowns (server rejects anything else). Hostel residents see only *their own block + Common Areas*. |

Replace the sample rows with your college's real buildings, rooms, blocks and student list.

## AI photo-relevance check (is the photo really of the problem?)
`relevance.py` sends each photo (with category + description) to a Claude vision model, which answers "relevant or not".
- **Irrelevant** (e.g. a bedroom photo for a projector complaint) → rejected on the spot with the reason; file deleted.
- Applies to student photos, to admin "after" photos, and to replacement photos.
- Admins can also press **Flag photo**; the reporter must upload a relevant one.
- **Turn it on:** `export ANTHROPIC_API_KEY=sk-ant-...` before `python app.py` (optional `CAMPUSLOOP_VISION_MODEL`, default `claude-haiku-5-5`).
- **Without a key** the app still works: photos are accepted but badged *"awaiting admin review"*. If the AI service is down it fails open the same way, so students are never blocked.

## How each requirement is met
| Problem-statement requirement | Implementation |
|---|---|
| Separate Classroom & Hostel modules | `/classroom`, `/hostel`, separate category sets (`services.CATEGORIES`) and location sets (`data/campus.csv`) |
| All students → classroom; **only hostel residents → hostel** | Roster-driven (`data/students.csv`), enforced on every list/report/detail/API route by `services.can_access_module`. No self-declared access. |
| Descriptions, locations, **mandatory photos where applicable** | Per-category `photo_required` flag, enforced server-side *and* in the form (📸 marks) |
| **Photographic evidence validation** | `relevance.py` (vision AI: is it the right kind of photo?) + `vision.py`: real decodable image, ≥200px, not blank/black, EXIF stripped; perceptual **dHash** rejects recycled photos; resolution "after" photo must differ from the "before" |
| **AI duplicate detection** | `ai_engine.find_similar`: TF-IDF cosine on synonym-folded, negation-normalised text (+25% location, +20% category). Different room numbers ⇒ different place. Live suggestions while typing + server-side gate. ≥70% match auto-merges; admin can split ("Not a duplicate") |
| **Recurring issues** | Same module+category+place fixed within 60 days ⇒ flagged ↻ recurring (+priority); Analytics → Hotspots |
| **Prioritise by urgency & collective impact** | `ai_engine.score_priority` (0-100): category severity + urgency language + collective impact (saturating curve over affected students) + waiting time + recurrence + failed-fix penalty. Labels Low/Medium/High/Critical |
| Explainable | Every complaint shows **"Why this priority?"** with each factor's points |
| Admin manages & gives **photographic resolution evidence** | Control room queue; *Start work*; *Resolve* needs note + "after" photo |
| **Student-verified resolution** | Affected students (reporter + "I face this too" + merged duplicates) vote Fixed/Not fixed. Majority closes; majority "not fixed" re-opens (+priority). 72 h of silence = consent. |

## Architecture
```
Browser (Jinja + vanilla JS live-similarity)
   │
Flask app.py ── routes, auth (hashed pw, sessions), CSRF, role guards
   ├─ services.py   business rules: merge, impact, priority recompute, resolve, verify, auto-close
   ├─ ai_engine.py  tokenizer + synonyms, TF-IDF similarity, urgency/priority model (explainable)
   ├─ vision.py     photo validation, dHash, normalise/save
   └─ db.py         SQLite schema: users, complaints, supporters, resolutions, verifications, activity
```
Data rules: registration, hostel access and locations all come from `data/*.csv`.
Security: password hashing (Werkzeug), per-session CSRF tokens on every POST, uploads served only to signed-in users
with strict filename pattern, 12 MB request cap, parameterised SQL, Jinja autoescape.

## 3-minute demo script
1. **Home** → stats (duplicates merged, fake fixes caught).
2. Sign in as **meera** (day scholar) → open *Hostel* → locked (roster-based access). Try registering with an email not in the roster → refused.
3. Sign in as **aarav** → *Report hostel issue* → pick Block A → *Floor 2 Washroom* (only his block + common areas are offered), *Water supply*, type "No water on second floor" → AI panel suggests **#7** live → "Same problem — add me" (impact ↑, priority ↑).
4. Report a classroom *Projector* issue with no photo → blocked (**mandatory evidence**); upload the same photo twice → rejected.
5. As aarav open **#10** → admin says fixed, show before/after photos → vote **Fixed**.
6. Sign in as **admin** → Control room: AI-ranked queue (fan sparking = **Critical**, recurring), flag the unchecked photo on **#5**, open **#4** → *Why this priority?*, resolve with an "after" photo.
7. **Analytics** → hotspots, merge count, % of fixes students rejected (#11 re-opened).

## Honest limitations / next steps
- AI is a transparent, offline heuristic (TF-IDF + rules), chosen for speed, privacy and explainability. `ai_engine.py` marks
  where an LLM/embedding model could re-rank candidates or summarise complaints.
- The photo-relevance AI needs an API key + internet (it was tested here with a mocked model; run it once with your key before the demo). It judges *what the photo shows*; it can't prove a real problem exists or catch a staged scene. Next: EXIF geotag/time check.
- Registration trusts roll number + email from the CSV (no email OTP). Next: college SSO / OTP, roster import from the ERP.
- Next: email/WhatsApp notifications, SLA timers per category, warden role per block, PostgreSQL + object storage, mobile PWA.
=======
# CAMPUSLOOP-AI
AI-powered campus and hostel complaint management platform
>>>>>>> 22b11c05d5cb2ce1f95f1ffd93e05b19351164e4
