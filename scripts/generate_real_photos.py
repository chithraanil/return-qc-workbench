"""Generate real photorealistic product photos via Vertex AI Imagen 3 (with clean studio fallback)."""

from __future__ import annotations

import io
import os
import pathlib
import shutil
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
MEDIA = ROOT / "data" / "media"
KNOWN = ROOT / "data" / "known_images" / "stock_bedsheet_web_image.jpg"

HERO_PROMPTS = {
    "C01_dispatch_front.jpg": "Overhead warehouse packing photo of a brand-new matte black smartphone in an open retail box with factory protective film, white barcode sticker clearly showing IMEI 358910094412, crisp lighting.",
    "C01_dispatch_imei.jpg": "Close-up macro photo of the back of a brand-new matte black smartphone showing a clean factory barcode sticker reading IMEI: 358910094412, spotless unblemished casing.",
    "C01_return_front.jpg": "Customer return inspection photo on a table of a heavily scratched, scuffed black smartphone with a chipped corner and worn screen, natural indoor light.",
    "C01_return_imei.jpg": "Close-up macro photo of the back of a scratched black smartphone showing a worn sticker reading a different serial number IMEI: 352001998831, visible scuffs.",
    "C03_dispatch.jpg": "Warehouse QC photo of a brand-new silver smartwatch with a pristine unblemished black glass screen inside a clean retail box.",
    "C03_return_screen.jpg": "Customer return photo of a silver smartwatch with a shattered spiderweb-cracked glass screen next to a crushed dented cardboard shipping box.",
    "C03_return_box.jpg": "Photo of a heavily crushed and torn brown cardboard shipping parcel box on a counter.",
    "C06_dispatch_soles.jpg": "Warehouse photo showing the underside soles of a brand-new unworn pair of white athletic running sneakers on a clean packing table, spotless white rubber treads.",
    "C06_return_soles.jpg": "Customer return photo showing the underside soles of white running sneakers heavily stained with mud, scuffed rubber heels, clearly worn outdoors.",
    "C10_dispatch_logo.jpg": "Luxury tan leather designer handbag in dustbag at warehouse inspection, crisp gold hardware emblem and straight even stitching.",
    "C10_return_logo.jpg": "Counterfeit-looking tan handbag returned by buyer with crooked stitching, dull brass hardware, and broken strap.",
    "C11_dispatch_wrapped.jpg": "Two glossy white ceramic coffee mugs neatly wrapped in protective bubble wrap inside a sturdy shipping box before dispatch.",
    "C11_return_cracked.jpg": "Broken ceramic coffee mug with a large crack and chipped rim next to a dented shipping box.",
    "C19_dispatch_size_label.jpg": "Pair of brand-new brown leather formal oxford shoes in open shoebox, size tag on tongue and box clearly printed UK 8.",
    "C19_return_size_label.jpg": "Customer photo of brand-new brown leather formal shoes showing the inner size label clearly printed UK 8 while order invoice next to it says Ordered: UK 9.",
}


def generate_imagen_photos():
  project = os.environ.get("GOOGLE_CLOUD_PROJECT", "agent-builder-ge")
  try:
    from google import genai
    client = genai.Client(vertexai=True, project=project, location="us-central1")
  except Exception as e:
    print("Skipping Imagen client init:", e)
    return
  for fname, prompt in HERO_PROMPTS.items():
    dest = MEDIA / fname
    try:
      res = client.models.generate_images(
          model="imagen-3.0-generate-002",
          prompt=prompt,
          config={"number_of_images": 1, "aspect_ratio": "4:3"},
      )
      img_bytes = res.generated_images[0].image.image_bytes
      Image.open(io.BytesIO(img_bytes)).convert("RGB").save(dest, "JPEG", quality=90)
      print(f"  [Imagen 3] Generated real photo: {fname}")
    except Exception as e:
      print(f"  [Fallback] {fname}: {e}")


def ensure_special_cases():
  # Ensure C04_return_blurry.jpg is genuinely blurry so Gemini 3.8 Flash triggers RETAKE
  p_blur = MEDIA / "C04_return_blurry.jpg"
  if p_blur.exists():
    im = Image.open(p_blur).convert("RGB").filter(ImageFilter.GaussianBlur(radius=14))
    im.save(p_blur, "JPEG", quality=75)
  # Ensure C13_return_stock.jpg matches known_images for deterministic pHash hit
  if KNOWN.exists():
    shutil.copyfile(KNOWN, MEDIA / "C13_return_stock.jpg")


if __name__ == "__main__":
  generate_imagen_photos()
  ensure_special_cases()
  print("Done generating product photos!")
