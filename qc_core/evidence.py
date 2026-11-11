"""Renders the side-by-side evidence image (reference vs return, boxes, verdict)."""

from __future__ import annotations

import pathlib
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from qc_core.schemas import Verdict

PANEL_H = 640
BANNER_H = 90
_COLORS = {"APPROVE": (22, 163, 74), "REJECT": (220, 38, 38),
           "REQUEST_MORE_EVIDENCE": (234, 88, 12), "HUMAN_REVIEW": (202, 138, 4)}


def _font(size: int):
  for name in ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial.ttf",
               "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
    try:
      return ImageFont.truetype(name, size)
    except OSError:
      continue
  return ImageFont.load_default(size=size)  # Pillow >= 10.1: scalable fallback


def _fit(img: Image.Image) -> Image.Image:
  img = img.convert("RGB")
  w = int(img.width * PANEL_H / img.height)
  return img.resize((w, PANEL_H))


def _box(draw, box, w, h, color, label):
  ymin, xmin, ymax, xmax = box
  xy = [xmin * w / 1000, ymin * h / 1000, xmax * w / 1000, ymax * h / 1000]
  draw.rectangle(xy, outline=color, width=5)
  draw.text((xy[0] + 6, max(0, xy[1] - 26)), label, fill=color, font=_font(20))


def render(verdict: Verdict, reference_img: Optional[Image.Image],
           return_img: Image.Image, out_path: str,
           reference_label: str = "DISPATCH (seller)") -> str:
  ret = _fit(return_img)
  ref = _fit(reference_img) if reference_img is not None else Image.new(
      "RGB", (ret.width, PANEL_H), (240, 240, 240))
  gap = 16
  canvas = Image.new("RGB", (ref.width + gap + ret.width, PANEL_H + BANNER_H + 40),
                     "white")
  canvas.paste(ref, (0, 40))
  canvas.paste(ret, (ref.width + gap, 40))
  d = ImageDraw.Draw(canvas)
  d.text((8, 8), reference_label, fill="black", font=_font(22))
  d.text((ref.width + gap + 8, 8), "RETURN (customer)", fill="black", font=_font(22))

  red = (220, 38, 38)
  for i, ev in enumerate(verdict.evidence, start=1):
    tag = f"{i}. {ev.code.value}"
    if ev.dispatch_box and reference_img is not None:
      sub = ImageDraw.Draw(ref)
      _box(sub, ev.dispatch_box, ref.width, ref.height, (37, 99, 235), str(i))
    if ev.return_box:
      sub = ImageDraw.Draw(ret)
      _box(sub, ev.return_box, ret.width, ret.height, red, tag)
  canvas.paste(ref, (0, 40))
  canvas.paste(ret, (ref.width + gap, 40))

  color = _COLORS.get(verdict.decision.value, (60, 60, 60))
  y0 = PANEL_H + 40
  d.rectangle([0, y0, canvas.width, canvas.height], fill=color)
  codes = ", ".join(c.value for c in verdict.reason_codes) or "no issue found"
  d.text((16, y0 + 10),
         f"{verdict.decision.value}  ·  {verdict.confidence:.0%}  ·  route: {verdict.route.value}",
         fill="white", font=_font(28))
  d.text((16, y0 + 52), f"{codes}  ·  basis: {verdict.comparison_basis.value}"
         f"  ·  liable: {verdict.liable_party.value}", fill="white", font=_font(20))

  pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
  canvas.save(out_path, quality=90)
  return out_path
