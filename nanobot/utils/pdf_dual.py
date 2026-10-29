"""Standalone PDF dual-read helpers (Persian/RTL text-layer normalization,
numeric tokens, page rendering).

Single source of truth shared by nanobot (``nanobot.utils.document``) and the
Open WebUI ``yoda_pdf_dual`` filter/tool (mounted read-only into that
container). Keep this module free of nanobot imports: stdlib only, plus
lazily-imported ``pymupdf`` / ``pypdf``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# PDF dual reading: page rendering, Persian/RTL normalization, numeric tokens
# ---------------------------------------------------------------------------

_PRESENTATION_FORM_RANGES = ((0xFB50, 0xFDFF), (0xFE70, 0xFEFF))
_ARABIC_INDIC_DIGITS = "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"
_PERSIAN_DIGITS = "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9"
_EASTERN_DIGITS = _ARABIC_INDIC_DIGITS + _PERSIAN_DIGITS
_DIGIT_TRANSLATION = str.maketrans(
    {**{d: str(i) for i, d in enumerate(_ARABIC_INDIC_DIGITS)},
     **{d: str(i) for i, d in enumerate(_PERSIAN_DIGITS)},
     "\u066b": ".",  # Arabic decimal separator
     "\u066c": ",",  # Arabic thousands separator
     "\u064a": "\u06cc",  # Arabic yeh -> Persian yeh
     "\u0649": "\u06cc",  # alef maksura -> Persian yeh
     "\u0643": "\u06a9"}  # Arabic kaf -> Persian keheh
)
_MIRROR = str.maketrans("()[]{}<>", ")(][}{><")
_EASTERN_DIGIT_RUN_RE = re.compile(f"[{_EASTERN_DIGITS}]+")
_NUMBER_TOKEN_RE = re.compile(r"\d(?:[\d,./]*\d)?")


def _is_presentation_form(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _PRESENTATION_FORM_RANGES)


def _is_arabic_letter(ch: str) -> bool:
    """Arabic-script letter or mark (digits excluded)."""
    if ch in _EASTERN_DIGITS or ch in "\u066b\u066c":
        return False
    cp = ord(ch)
    in_block = (
        0x0600 <= cp <= 0x06FF
        or 0x0750 <= cp <= 0x077F
        or 0x08A0 <= cp <= 0x08FF
        or _is_presentation_form(ch)
    )
    return in_block and unicodedata.category(ch)[0] in {"L", "M"}


def detect_visual_order(raw: str) -> bool:
    """True when a PDF text layer looks like visual-order (presentation-form) Persian.

    pypdf returns such text with each Arabic word in logical order, but the
    words of a line in reversed (left-to-right visual) order and every run of
    Eastern digits reversed (``1405`` comes out as ``۵۰۴۱``).
    """
    letters = [ch for ch in raw if _is_arabic_letter(ch)]
    if len(letters) < 5:
        return False
    forms = sum(1 for ch in letters if _is_presentation_form(ch))
    return forms / len(letters) >= 0.5


def _reverse_digit_runs(text: str) -> str:
    return _EASTERN_DIGIT_RUN_RE.sub(lambda m: m.group(0)[::-1], text)


def _fix_visual_token(token: str) -> str:
    """Convert one whitespace-delimited token from visual to logical order."""
    segments: list[str] = []
    current = ""
    current_is_letter: bool | None = None
    for ch in token:
        is_letter = _is_arabic_letter(ch)
        if current and is_letter != current_is_letter:
            segments.append(current)
            current = ""
        current += ch
        current_is_letter = is_letter
    if current:
        segments.append(current)
    has_letters = any(_is_arabic_letter(seg[0]) for seg in segments)
    fixed = [
        seg if _is_arabic_letter(seg[0]) else _reverse_digit_runs(seg).translate(_MIRROR)
        for seg in segments
    ]
    if has_letters and len(fixed) > 1:
        fixed.reverse()
    return "".join(fixed)


_LINE_START_PUNCT = ".\u060c\u061b!\u061f"  # . ، ؛ ! ؟


def _line_looks_logical(tokens: list[str]) -> bool:
    """Some lines (e.g. "تاریخ: ..." header labels) are emitted in logical order.

    A first token that is an Arabic-script label ending in ':' marks a logical
    line; a line starting with sentence punctuation is visual. Otherwise the
    document-level visual order applies.
    """
    first = tokens[0]
    if first[0] in _LINE_START_PUNCT:
        return False
    if not any(_is_arabic_letter(ch) for ch in first):
        return False
    return first.endswith(":") or (len(tokens) > 1 and tokens[1] == ":")


def _fix_visual_line(line: str) -> str:
    tokens = line.split()
    if not tokens:
        return ""
    if _line_looks_logical(tokens):
        # Word order is already logical; only Eastern-digit runs come out reversed.
        return " ".join(_reverse_digit_runs(tok) for tok in tokens)
    fixed = [_fix_visual_token(tok) for tok in tokens]
    if any(_is_arabic_letter(ch) for ch in line):
        fixed.reverse()
    return " ".join(fixed)


@dataclass(frozen=True, slots=True)
class NormalizedPdfText:
    text: str
    raw: str
    visual_order_fixed: bool


def normalize_persian_text(raw: str, *, visual_order: bool | None = None) -> NormalizedPdfText:
    """Deterministically normalize a Persian/Arabic PDF text layer.

    - Detect visual-order extraction (presentation forms) and, when found,
      reverse the word order of each line and every Eastern-digit run, and
      mirror brackets.
    - NFKC (presentation forms -> base letters, ligatures split).
    - Arabic yeh/kaf -> Persian, all Persian/Arabic-Indic digits -> ASCII.
    """
    visual = detect_visual_order(raw) if visual_order is None else visual_order
    text = raw.replace("\r\n", "\n")
    if visual:
        text = "\n".join(_fix_visual_line(line) for line in text.split("\n"))
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_DIGIT_TRANSLATION)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return NormalizedPdfText(text=text, raw=raw, visual_order_fixed=visual)


def extract_numeric_tokens(text: str, *, limit: int = 300) -> list[str]:
    """Return unique numeric tokens (prices, dates, codes) in reading order."""
    normalized = text.translate(_DIGIT_TRANSLATION)
    seen: dict[str, None] = {}
    for match in _NUMBER_TOKEN_RE.finditer(normalized):
        token = match.group(0)
        if token not in seen:
            seen[token] = None
            if len(seen) >= limit:
                break
    return list(seen)


def _canonical_number(value: str) -> str:
    """Comparable form of a number: ASCII digits, no grouping commas or spaces."""
    v = str(value).translate(_DIGIT_TRANSLATION).strip()
    v = re.sub(r"[\s,\u066c\u060c]", "", v)
    if re.fullmatch(r"\d+\.0+", v):
        v = v.split(".")[0]
    return v


def find_numbers_in_text(text: str, values: list[str]) -> dict[str, bool]:
    """Check which ``values`` occur as whole numeric tokens in ``text``."""
    tokens = {_canonical_number(t) for t in extract_numeric_tokens(text, limit=100_000)}
    # Also index the parts of dates/ranges ("1405/08/05" -> 1405, 08, 05, 1405/08/05).
    for token in list(tokens):
        for part in re.split(r"[/.]", token):
            if part:
                tokens.add(part)
    return {value: _canonical_number(value) in tokens for value in values}


def render_pdf_pages(path: str | Path, pages: list[int], dpi: int = 200) -> list[bytes]:
    """Render 0-based ``pages`` of a PDF to PNG bytes (requires PyMuPDF)."""
    import pymupdf

    images: list[bytes] = []
    with pymupdf.open(str(path)) as doc:
        for index in pages:
            pix = doc[index].get_pixmap(dpi=dpi)
            images.append(pix.tobytes("png"))
    return images


def extract_page_texts(path: str | Path, max_pages: int = 200) -> tuple[list[str], int]:
    """Raw pypdf text per page (first ``max_pages``) and the total page count."""
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    total = len(reader.pages)
    return [(reader.pages[i].extract_text() or "").strip() for i in range(min(total, max_pages))], total
