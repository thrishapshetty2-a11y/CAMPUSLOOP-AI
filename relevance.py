"""Is this photo actually evidence of the complaint?  (vision-model check)

Sends the photo + category + description to a Claude vision model and asks for a strict JSON verdict.
Set ANTHROPIC_API_KEY to enable (optional: CAMPUSLOOP_VISION_MODEL, default claude-haiku-5-5).

Returns (status, reason):
  'verified'   the model says the photo shows the reported kind of problem / the fixed item
  'irrelevant' the model says it does not (e.g. a bedroom photo for a projector complaint) -> rejected
  'unchecked'  no API key or the call failed -> accepted but marked "awaiting admin review"
"""
import base64
import json
import os
import re
import urllib.request

MODEL = os.environ.get("CAMPUSLOOP_VISION_MODEL", "claude-haiku-5-5")
API = "https://api.anthropic.com/v1/messages"


def enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _prompt(kind, module, category, title, description, location):
    place = "a college classroom/lab building" if module == "classroom" else "a college hostel (residence hall)"
    if kind == "after":
        task = ("This is the AFTER photo uploaded by facilities staff to prove the issue below was fixed. "
                "Answer relevant=true only if it plausibly shows the same kind of item/area in working or repaired condition.")
    else:
        task = ("This is the BEFORE photo a student uploaded as evidence of the issue below. "
                "Answer relevant=true only if the photo plausibly shows the kind of place/object/problem described "
                "(e.g. a projector, fan, bench, tap, washroom, mess food). Answer false for selfies, homes/bedrooms "
                "unrelated to the complaint, memes, screenshots, pets, random objects, or unrelated scenery.")
    return (f"You verify photo evidence for a campus complaint system at {place}.\n{task}\n"
            f"Category: {category}\nTitle: {title}\nDescription: {description}\nLocation: {location}\n"
            'Reply with ONLY JSON: {"relevant": true|false, "reason": "<one short sentence for the student>"}')


def parse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    d = json.loads(m.group(0))
    return bool(d["relevant"]), str(d.get("reason", ""))[:200]


def check(path, module, category, title, description, location, kind="before", timeout=25):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return "unchecked", "AI photo check is not configured; an admin will review the photo."
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        body = json.dumps({
            "model": MODEL, "max_tokens": 200,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": _prompt(kind, module, category, title, description, location)}]}]}).encode()
        req = urllib.request.Request(API, body, {"x-api-key": key, "anthropic-version": "2023-06-01",
                                                  "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        ok, reason = parse("".join(b.get("text", "") for b in data["content"]))
        return ("verified" if ok else "irrelevant"), reason
    except Exception as e:  # network, quota, bad JSON ... never block a student because the checker is down
        return "unchecked", "AI photo check unavailable; an admin will review the photo."
