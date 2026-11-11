"""Cheap deterministic checks: perceptual-hash reuse and EXIF anomalies."""

from __future__ import annotations

import datetime as dt
import io
import pathlib
from typing import Iterable, Optional

import imagehash
from PIL import ExifTags, Image

from qc_core.schemas import DeterministicSignals

# Hamming distance at or below this = "same picture" (robust to resize/recompress).
PHASH_THRESHOLD = 8
_EDITING_SOFTWARE = ("photoshop", "snapseed", "picsart", "lightroom", "gimp",
                     "canva", "facetune", "remini")


def load_image(path_or_bytes) -> Image.Image:
  if isinstance(path_or_bytes, (bytes, bytearray)):
    return Image.open(io.BytesIO(path_or_bytes))
  return Image.open(path_or_bytes)


def phash(img: Image.Image) -> imagehash.ImageHash:
  return imagehash.phash(img.convert("RGB"))


class ReuseIndex:
  """In-memory index of known images (stock photos, earlier claims).

  Production: store hashes in BigQuery/Firestore keyed by hash prefix.
  """

  def __init__(self):
    self._entries: list[tuple[imagehash.ImageHash, str]] = []

  def add(self, img: Image.Image, ref: str) -> None:
    self._entries.append((phash(img), ref))

  def add_dir(self, directory: str) -> None:
    for p in sorted(pathlib.Path(directory).glob("*")):
      if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
        self.add(load_image(str(p)), p.name)

  def lookup(self, img: Image.Image) -> Optional[str]:
    h = phash(img)
    best = None
    for other, ref in self._entries:
      d = h - other
      if d <= PHASH_THRESHOLD and (best is None or d < best[0]):
        best = (d, ref)
    return best[1] if best else None


def _exif(img: Image.Image) -> dict:
  raw = img.getexif() or {}
  out = {ExifTags.TAGS.get(k, k): v for k, v in raw.items()}
  try:
    sub = raw.get_ifd(0x8769)  # Exif sub-IFD (DateTimeOriginal lives here)
    out.update({ExifTags.TAGS.get(k, k): v for k, v in sub.items()})
  except Exception:  # pylint: disable=broad-except
    pass
  return out


def exif_signals(img: Image.Image,
                 delivered_at: Optional[str]) -> tuple[bool, list[str]]:
  """Returns (anomaly, notes). Missing EXIF alone is NOT an anomaly."""
  notes: list[str] = []
  anomaly = False
  ex = _exif(img)
  if not ex:
    notes.append("No EXIF metadata (common for screenshots/messaging apps).")
    return False, notes
  software = str(ex.get("Software", "")).lower()
  if any(s in software for s in _EDITING_SOFTWARE):
    anomaly = True
    notes.append(f"Edited with: {ex.get('Software')}")
  taken = ex.get("DateTimeOriginal") or ex.get("DateTime")
  if taken and delivered_at:
    try:
      taken_dt = dt.datetime.strptime(str(taken), "%Y:%m:%d %H:%M:%S").date()
      deliv = dt.date.fromisoformat(delivered_at[:10])
      if taken_dt < deliv:
        anomaly = True
        notes.append(
            f"Photo taken {taken_dt} BEFORE delivery date {deliv}.")
    except ValueError:
      notes.append(f"Unparseable capture date: {taken}")
  return anomaly, notes


def run_checks(return_images: Iterable[Image.Image],
               delivered_at: Optional[str],
               reuse_index: Optional[ReuseIndex]) -> DeterministicSignals:
  sig = DeterministicSignals()
  for img in return_images:
    if reuse_index is not None:
      ref = reuse_index.lookup(img)
      if ref:
        sig.phash_reuse = True
        sig.phash_match_ref = ref
    anomaly, notes = exif_signals(img, delivered_at)
    sig.exif_anomaly = sig.exif_anomaly or anomaly
    sig.exif_notes.extend(notes)
  return sig
