"""Generates labelled PLACEHOLDER images so the pipeline can be tested before
real photos are shot. Replace files in data/media/ with real captures.

Special cases wired for deterministic checks:
  C13 return = copy of a 'stock' image that is also in data/known_images/ (pHash hit)
  C16 return = EXIF Software=Photoshop, DateTimeOriginal before delivery (EXIF hit)
"""

from __future__ import annotations

import pathlib
import random
import sys

import yaml
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
MEDIA = ROOT / "data" / "media"
KNOWN = ROOT / "data" / "known_images"


def _font(size):
  try:
    return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
  except OSError:
    return ImageFont.load_default(size=size)


def placeholder(name: str, seed: int) -> Image.Image:
  rnd = random.Random(seed)
  base = tuple(rnd.randint(90, 200) for _ in range(3))
  img = Image.new("RGB", (900, 1200), base)
  d = ImageDraw.Draw(img)
  for _ in range(25):  # texture so pHash differs per image
    x, y = rnd.randint(0, 900), rnd.randint(0, 1200)
    r = rnd.randint(20, 160)
    d.ellipse([x - r, y - r, x + r, y + r],
              fill=tuple(rnd.randint(0, 255) for _ in range(3)))
  d.rectangle([40, 520, 860, 680], fill="white")
  d.text((60, 550), "PLACEHOLDER", fill="black", font=_font(48))
  d.text((60, 615), name, fill="black", font=_font(32))
  return img


def main():
  MEDIA.mkdir(parents=True, exist_ok=True)
  KNOWN.mkdir(parents=True, exist_ok=True)
  doc = yaml.safe_load((ROOT / "data" / "claims.yaml").read_text())
  names = []
  for c in doc["claims"]:
    for key in ("dispatch", "return", "catalog"):
      names += c.get(key, []) or []
  made = 0
  for i, n in enumerate(names):
    p = MEDIA / n
    if p.exists() or not n.lower().endswith((".jpg", ".jpeg", ".png")):
      continue
    img = placeholder(n, seed=i)
    if n == "C13_return_stock.jpg":
      stock = placeholder("stock_bedsheet_web_image", seed=999)
      stock.save(KNOWN / "stock_bedsheet_web_image.jpg", quality=92)
      img = stock.resize((870, 1160))  # recompressed/resized copy
    if n == "C16_return_edited.jpg":
      exif = Image.Exif()
      exif[0x0131] = "Adobe Photoshop 26.1"        # Software
      exif[0x0132] = "2026:10:15 18:22:01"         # DateTime (before 2026-10-20)
      img.save(p, quality=90, exif=exif)
      made += 1
      continue
    img.save(p, quality=90)
    made += 1
  print(f"Created {made} placeholder images in {MEDIA}")
  vids = [n for n in names if n.lower().endswith(".mp4")]
  print(f"Videos to capture for live runs ({len(vids)}): {', '.join(vids)}")


if __name__ == "__main__":
  sys.exit(main())
