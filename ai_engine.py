"""CampusLoop AI engine — explainable, dependency-free NLP for complaints.

Three jobs:
  1. Duplicate / related-complaint detection  (TF-IDF cosine + location + category)
  2. Priority scoring from urgency, severity, collective impact, age, recurrence
  3. Every score ships with human-readable reasons (explainable AI, no black box)

Runs fully offline. `LLM_HOOK` at the bottom shows where a hosted model could be
plugged in to re-rank candidates; the project works without it.
"""
import math
import re
from collections import Counter

STOP = set("""a an the is are was were be been being of to in on at for and or but with without from by it its this that
these those there here has have had do does did not no nor so very too can could should would will just please sir
madam kindly again also since still now today yesterday our my we i you they them their near room""".split())

# Light synonym folding so "tubelight", "bulb" and "lamp" all mean the same thing.
SYN = {
    "tubelight": "light", "bulb": "light", "lamp": "light", "lights": "light", "led": "light", "lighting": "light",
    "wifi": "internet", "wi-fi": "internet", "network": "internet", "wlan": "internet", "router": "internet", "broadband": "internet",
    "leaking": "leak", "leakage": "leak", "dripping": "leak", "drip": "leak", "seepage": "leak", "tap": "leak",
    "toilet": "washroom", "bathroom": "washroom", "restroom": "washroom", "latrine": "washroom", "urinal": "washroom",
    "commode": "washroom", "flush": "washroom",
    "chair": "furniture", "desk": "furniture", "bench": "furniture", "table": "furniture", "cot": "furniture",
    "bed": "furniture", "cupboard": "furniture", "almirah": "furniture", "locker": "furniture",
    "projector": "projector", "screen": "projector", "display": "projector", "hdmi": "projector", "smartboard": "projector",
    "ac": "cooling", "aircon": "cooling", "cooler": "cooling", "ventilation": "cooling", "fan": "fan", "fans": "fan",
    "geyser": "heater", "heater": "heater",
    "cockroach": "pest", "cockroaches": "pest", "rat": "pest", "rats": "pest", "mouse": "pest", "mice": "pest",
    "insect": "pest", "insects": "pest", "mosquito": "pest", "mosquitoes": "pest", "bedbug": "pest", "bedbugs": "pest",
    "ant": "pest", "ants": "pest", "lizard": "pest",
    "dirty": "unclean", "filthy": "unclean", "garbage": "unclean", "trash": "unclean", "dustbin": "unclean",
    "smelly": "unclean", "stink": "unclean", "stinks": "unclean", "unhygienic": "unclean",
    "broken": "broken", "damaged": "broken", "cracked": "broken", "faulty": "broken", "defective": "broken",
    "malfunctioning": "broken", "dead": "broken", "stopped": "broken", "notworking": "broken",
    "food": "mess", "meal": "mess", "meals": "mess", "canteen": "mess", "dining": "mess", "breakfast": "mess",
    "lunch": "mess", "dinner": "mess", "mess": "mess",
    "electricity": "power", "power": "power", "outage": "power", "blackout": "power", "socket": "power",
    "switch": "power", "plug": "power", "wiring": "power",
}


def stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def tokenize(text):
    text = (text or "").lower().replace("wi-fi", "wifi")
    text = re.sub(r"(does ?n.?t|do ?n.?t|did ?n.?t|won.?t|is ?n.?t|are ?n.?t|not|never|unable to)\s+"
                  r"(work(?:ing)?|function(?:ing)?|turn(?:ing)?|start(?:ing)?|switch(?:ing)?|respond(?:ing)?|"
                  r"come on|coming on|power(?:ing)?|run(?:ning)?|display(?:ing)?|open(?:ing)?)\b(\s+on\b)?", " broken ", text)
    text = re.sub(r"(stopped|stops|stop)\s+working|out of order|no display|blank screen|screen stays blank|stays blank", " broken ", text)
    out = []
    for w in re.findall(r"[a-z0-9\-]+", text):
        if w in STOP:
            continue
        w = SYN.get(w, w)
        w = SYN.get(stem(w), stem(w))
        if len(w) > 1:
            out.append(w)
    return out


def loc_tokens(loc):
    toks = re.findall(r"[a-z]+|\d+", (loc or "").lower())
    toks = [t for t in toks if t not in {"room", "floor", "hall", "the", "of", "in", "no", "number", "st", "nd", "rd", "th"}]
    return set(toks)


def loc_similarity(a, b):
    if a and b and "," in a and "," in b:  # dataset format "Building, Place"
        (ba, pa), (bb, pb) = (x.lower().split(",", 1) for x in (a, b))
        if ba.strip() != bb.strip():
            return 0.0  # different building = different place
        if pa.strip() == pb.strip():
            return 1.0
        a, b = pa, pb
    ta, tb = loc_tokens(a), loc_tokens(b)
    if not ta or not tb:
        return 0.5  # unknown location: neutral
    na, nb = {t for t in ta if t.isdigit()}, {t for t in tb if t.isdigit()}
    if na and nb and not (na & nb):
        return 0.0  # different room / floor numbers → different place
    return len(ta & tb) / len(ta | tb)


def _tfidf_vectors(docs):
    toks = [tokenize(d) for d in docs]
    n = len(docs)
    df = Counter()
    for t in toks:
        df.update(set(t))
    idf = {w: math.log((n + 1) / (c + 1)) + 1 for w, c in df.items()}
    vecs = []
    for t in toks:
        tf = Counter(t)
        v = {w: (1 + math.log(c)) * idf[w] for w, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({w: x / norm for w, x in v.items()})
    return vecs


def _cos(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(w, 0.0) for w, x in a.items())


def find_similar(new, candidates, limit=3, min_score=0.40):
    """new: dict(title, description, location, category)
    candidates: list of dict(id, title, description, location, category, ...)
    returns [(candidate, score, reasons)] best first."""
    if not candidates:
        return []
    docs = [f"{new['title']} {new['title']} {new['description']}"] + \
           [f"{c['title']} {c['title']} {c['description']}" for c in candidates]
    vecs = _tfidf_vectors(docs)
    res = []
    for c, v in zip(candidates, vecs[1:]):
        text = _cos(vecs[0], v)
        loc = loc_similarity(new["location"], c["location"])
        cat = 1.0 if new["category"] == c["category"] else 0.0
        score = 0.55 * text + 0.25 * loc + 0.20 * cat
        if loc < 0.2:  # clearly a different place → probably a different problem
            score *= 0.55
        if score >= min_score:
            reasons = []
            if text >= 0.45:
                reasons.append(f"wording is {int(text * 100)}% similar")
            if loc >= 0.5:
                reasons.append("same location")
            if cat:
                reasons.append("same category")
            res.append((c, round(score, 2), reasons or ["related wording"]))
    res.sort(key=lambda x: -x[1])
    return res[:limit]


# ---------------------------------------------------------------- priority
CATEGORY_SEVERITY = {  # base severity 1-10
    "Safety hazard": 9, "Water supply": 8, "Electrical": 7, "Plumbing / Leakage": 6, "Pest control": 6,
    "Mess / Food quality": 6, "Security": 8, "Projector / AV": 5, "Cooling / Ventilation": 5, "Wi-Fi / Network": 4,
    "Cleanliness / Hygiene": 4, "Cleanliness": 4, "Furniture": 3, "Furniture / Room": 3, "Noise": 3, "Other": 2,
}

URGENCY = [  # (regex, points, label)
    (r"spark|short.?circuit|electric(al)? shock|exposed wire|live wire", 20, "electrical danger"),
    (r"\bfire\b|smoke|burning|burnt", 20, "fire / smoke"),
    (r"food poison|vomit|diarr|fever|\bsick\b|\bill\b|hospital", 15, "health risk"),
    (r"unsafe|danger|injur|broken glass|collaps|falling|fell\b|sharp|harass|theft|stolen", 15, "safety risk"),
    (r"flood|overflow|sewage|drain.*block|blocked drain", 14, "flooding / sewage"),
    (r"no water|without water|water shortage|water.*not coming|no electricity|power cut|blackout", 12, "essential service down"),
    (r"snake|infest|bedbug|bed bug", 8, "pest infestation"),
    (r"mold|mould|fung", 5, "mould"),
    (r"leak", 5, "leakage"),
    (r"exam|viva|practical|\btest\b|lab session|quiz", 6, "academic impact"),
    (r"urgent|emergency|immediately|asap|critical", 6, "marked urgent"),
    (r"\d+\s*(days|weeks)|since (last )?(week|month)|for (a )?(week|month)", 4, "long-standing"),
]


def urgency_points(text):
    pts, why = 0, []
    t = (text or "").lower()
    for rx, p, label in URGENCY:
        if re.search(rx, t):
            pts += p
            why.append(label)
    return min(pts, 30), why


def label_for(score):
    return "Critical" if score >= 65 else "High" if score >= 45 else "Medium" if score >= 25 else "Low"


def score_priority(category, title, description, impact, age_days, recurring, reopen_count):
    """Returns (score 0-100, label, factors[list of (name, points, detail)])."""
    sev = CATEGORY_SEVERITY.get(category, 3)
    f = [("Category severity", round(sev * 3.5, 1), f"{category} (base {sev}/10)")]
    u, why = urgency_points(f"{title}. {description}")
    if u:
        f.append(("Urgency language", u, ", ".join(why)))
    imp = 25 * (1 - math.exp(-(max(impact, 1) - 1) / 6))
    if impact > 1:
        f.append(("Collective impact", round(imp, 1), f"{impact} students affected"))
    age = min(10.0, age_days * 1.5)
    if age >= 1:
        f.append(("Waiting time", round(age, 1), f"open for {age_days:.1f} days"))
    if recurring:
        f.append(("Recurring issue", 10, "same problem was fixed here recently"))
    if reopen_count:
        f.append(("Failed resolution", min(16, 8 * reopen_count), f"students rejected the fix {reopen_count}x"))
    score = min(100.0, sum(p for _, p, _ in f))
    return round(score, 1), label_for(score), f


# LLM_HOOK: to use a hosted model, re-rank `find_similar` output or add a
# one-line "AI summary" per complaint here. Intentionally omitted so the demo
# is deterministic and runs with zero API keys.
