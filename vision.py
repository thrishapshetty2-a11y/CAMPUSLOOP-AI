"""Photographic evidence validation (Pillow only).

Checks: real decodable image, minimum size, not blank/black, normalises + strips EXIF,
and a perceptual dHash so recycled photos are caught.
"""
import os
import uuid
from PIL import Image, ImageOps, ImageStat

ALLOWED = {"JPEG", "PNG", "WEBP", "MPO"}
MAX_BYTES = 8 * 1024 * 1024


class PhotoError(ValueError):
    pass


def dhash(img, size=8):
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.tobytes())
    bits = 0
    for r in range(size):
        for c in range(size):
            bits = (bits << 1) | (1 if px[r * (size + 1) + c] > px[r * (size + 1) + c + 1] else 0)
    return f"{bits:016x}"


def hamming(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def validate_and_save(file_storage, upload_dir, known_hashes=(), reject_if_near=None):
    """Returns (filename, hash). known_hashes: hashes that must NOT be re-used (recycled photo).
    reject_if_near: a 'before' hash the new photo must visibly differ from (for resolution proof)."""
    raw = file_storage.read()
    if not raw:
        raise PhotoError("No photo received.")
    if len(raw) > MAX_BYTES:
        raise PhotoError("Photo is larger than 8 MB.")
    file_storage.stream.seek(0)
    try:
        img = Image.open(file_storage.stream)
        fmt = img.format
        img.load()
    except Exception:
        raise PhotoError("That file is not a valid image.")
    if fmt not in ALLOWED:
        raise PhotoError("Use a JPG, PNG or WebP photo.")
    img = ImageOps.exif_transpose(img).convert("RGB")
    if min(img.size) < 200:
        raise PhotoError("Photo is too small (min 200×200 px) — please take a clearer picture.")
    if ImageStat.Stat(img.convert("L")).stddev[0] < 6:
        raise PhotoError("Photo looks blank or too dark — please retake it.")
    h = dhash(img)
    for k in known_hashes:
        if k and hamming(h, k) <= 3:
            raise PhotoError("This photo was already submitted with another report. Please upload a fresh photo.")
    if reject_if_near and hamming(h, reject_if_near) <= 5:
        raise PhotoError("Resolution photo looks identical to the original problem photo — upload an 'after' photo.")
    img.thumbnail((1280, 1280))
    name = uuid.uuid4().hex + ".jpg"
    os.makedirs(upload_dir, exist_ok=True)
    img.save(os.path.join(upload_dir, name), "JPEG", quality=85)
    return name, h
