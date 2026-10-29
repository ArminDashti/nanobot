"""Dual PDF reading: Persian normalization, text + image blocks, number verification."""

import base64

import pytest

from nanobot.agent.tools.filesystem import ReadFileTool, VerifyPdfNumbersTool
from nanobot.utils.document import (
    detect_visual_order,
    extract_numeric_tokens,
    find_numbers_in_text,
    normalize_persian_text,
)

# Real pypdf output from an Ondpline letter (presentation forms, visual order).
RAW_DATE = "ﺗﺎﺭﯾﺦ: ٥٠٤١/٨٠/٥٠"
RAW_GREETING = "ﺍﺣﺘﺮﺍﻡ ﻭ ﺳﻼﻡ ﺑﺎ"
RAW_ROW = "12,800,000 11,636,364 9,696,970 8,658,009 ﻉ ٠٣ ﺑﻄﺮﯼ| ﺳﯽ ﻭﯾﺘﺎﻣﯿﻦ+ﮐﻼﮊﻥ ﮐﭙﺴﻮﻝ| ﻧﻮﺗﺮﯾﺸﻦ ﻣﺎﯼ"
RAW_PAREN = "(ﺭﯾﺎﻝ)"


def test_detects_visual_order_presentation_forms():
    assert detect_visual_order(RAW_GREETING)
    assert not detect_visual_order("با سلام و احترام")
    assert not detect_visual_order("Hello world 123")


def test_reversed_eastern_digits_1405():
    out = normalize_persian_text(RAW_DATE).text
    assert "1405/08/05" in out
    assert "5041" not in out
    assert out == "تاریخ: 1405/08/05"


def test_reversed_word_order():
    out = normalize_persian_text(RAW_GREETING)
    assert out.visual_order_fixed
    assert out.text == "با سلام و احترام"
    assert out.raw == RAW_GREETING


def test_table_row_order_and_latin_numbers_kept():
    out = normalize_persian_text(RAW_ROW).text
    assert out.startswith("مای نوتریشن")
    assert "30 ع" in out
    # Latin digit groups are not reversed; RTL column order: name, then prices.
    assert out.endswith("8,658,009 9,696,970 11,636,364 12,800,000")


def test_sentence_line_reversed_with_leading_period():
    out = normalize_persian_text(".ﮔﺮﺩﺩ ﻣﯽ ﺍﯾﻔﺎﺩ ﺣﻀﻮﺭﺗﺎﻥ ﺑﻪ").text
    assert out == "به حضورتان ایفاد می گردد."


def test_label_line_with_spaced_colon_kept_logical():
    out = normalize_persian_text("ﺷﻤﺎﺭﻩ : ٥٠٤١ /ﺕ ﺍﻟﻒ/ ١١٨٨ ﭘﯿﻮﺳﺖ").text
    assert out.startswith("شماره : 1405 ")
    assert out.endswith("8811 پیوست")  # every Eastern-digit run is reversed


def test_brackets_mirrored():
    assert normalize_persian_text(RAW_PAREN).text == "(ریال)"


def test_logical_text_untouched_except_digits_and_letters():
    out = normalize_persian_text("قيمت ۱۲۳ تومان كالا", visual_order=False).text
    assert out == "قیمت 123 تومان کالا"


def test_numeric_tokens_and_lookup():
    text = normalize_persian_text(RAW_DATE + "\n" + RAW_ROW).text
    nums = extract_numeric_tokens(text)
    assert "1405/08/05" in nums and "12,800,000" in nums
    found = find_numbers_in_text(text, ["12800000", "۱۲,۸۰۰,۰۰۰", "1405", "12,900,000"])
    assert found == {"12800000": True, "۱۲,۸۰۰,۰۰۰": True, "1405": True, "12,900,000": False}


def _make_pdf(path, pages):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
        else:
            page.draw_rect(pymupdf.Rect(50, 50, 200, 200), color=(0, 0, 0), fill=(0, 0, 0))
    doc.save(str(path))
    doc.close()


def _texts(result):
    return "\n".join(b["text"] for b in result if b.get("type") == "text")


@pytest.mark.asyncio
async def test_read_file_pdf_returns_text_and_image(tmp_path):
    pdf = tmp_path / "letter.pdf"
    _make_pdf(pdf, ["Price 12,800,000 date 1405/08/05"])
    result = await ReadFileTool(workspace=tmp_path).execute(path=str(pdf))
    assert isinstance(result, list)
    text = _texts(result)
    assert "[page 1: text layer (machine-extracted)]" in text
    assert "12,800,000" in text
    assert "[page 1: rendered image" in text
    assert "[PDF cross-check rules]" in text
    images = [b for b in result if b.get("type") == "image_url"]
    assert len(images) == 1
    url = images[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_read_file_pdf_scanned_page_image_only(tmp_path):
    pdf = tmp_path / "scan.pdf"
    _make_pdf(pdf, [""])
    result = await ReadFileTool(workspace=tmp_path).execute(path=str(pdf))
    text = _texts(result)
    assert "NO text layer" in text
    assert "text layer (machine-extracted)" not in text
    assert any(b.get("type") == "image_url" for b in result)


@pytest.mark.asyncio
async def test_read_file_pdf_image_cap(tmp_path, monkeypatch):
    pdf = tmp_path / "many.pdf"
    _make_pdf(pdf, [f"Page {i}" for i in range(1, 8)])
    tool = ReadFileTool(workspace=tmp_path)
    monkeypatch.setattr(ReadFileTool, "_MAX_PDF_IMAGE_PAGES", 2)
    result = await tool.execute(path=str(pdf))
    assert sum(b.get("type") == "image_url" for b in result) == 2
    assert "image cap 2 pages" in _texts(result)
    one = await tool.execute(path=str(pdf), pages="6")
    assert sum(b.get("type") == "image_url" for b in one) == 1
    assert "Page 6" in _texts(one)


@pytest.mark.asyncio
async def test_verify_pdf_numbers(tmp_path):
    pdf = tmp_path / "v.pdf"
    _make_pdf(pdf, ["Total 4,700,000 and 3,179,113"])
    out = await VerifyPdfNumbersTool(workspace=tmp_path).execute(
        path=str(pdf), values=["4700000", "3,179,113", "3,179,114"],
    )
    assert "OK      4700000" in out
    assert "MISSING 3,179,114" in out