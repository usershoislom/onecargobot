import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from bot import ocr


def test_select_pages_keeps_everything_when_it_fits():
    out = ocr.select_pages(["intro", "rates 0.70"], budget=1000)
    assert out == "--- page 1 ---\nintro\n--- page 2 ---\nrates 0.70"


def test_select_pages_prefers_pages_with_numbers_and_keywords():
    filler = "lorem ipsum dolor sit amet " * 20
    pages = [
        "AGREEMENT between carrier and driver " + filler,
        filler,
        "EXHIBIT A compensation: 0.70 loaded, 0.35 empty, $150.00 detention " + filler,
        filler,
    ]
    out = ocr.select_pages(pages, budget=1300)
    assert "--- page 1 ---" in out and "--- page 3 ---" in out
    assert "--- page 2 ---" not in out and "--- page 4 ---" not in out
    assert "left out" in out


@pytest.mark.skipif(not ocr.tesseract_available(), reason="tesseract not installed")
def test_image_ocr_reads_text():
    img = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=48)
    except TypeError:  # older Pillow
        font = ImageFont.load_default()
    draw.text((20, 60), "LUMPER RECEIPT $185.00", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    text = ocr.image_text(buf.getvalue())
    assert "LUMPER" in text.upper() and "185" in text


def test_image_text_survives_garbage():
    assert ocr.image_text(b"not an image") == ""
