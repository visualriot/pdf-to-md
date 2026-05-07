#!/usr/bin/env python3
"""
PDF → Markdown converter.

Usage:
    python convert.py               # converts all PDFs in input/
    python convert.py path/to.pdf   # converts a single file

Output goes to output/ with the same filename, .md extension.
Each file gets YAML frontmatter with title, authors, and keywords,
followed by the article body with headings, abstract, and sections.
Headers, footers, page numbers, and running headers are removed.
"""

import re
import sys
from pathlib import Path
from collections import defaultdict

import fitz  # PyMuPDF
import pymupdf4llm


# ── Directories ───────────────────────────────────────────────────────────────

INPUT_DIR = Path("input")
OUTPUT_DIR = Path("output")

# ── Zone thresholds (fraction of page height/width) ───────────────────────────

HEADER_FRAC = 0.10   # top 10% = header zone
FOOTER_FRAC = 0.88   # below 88% = footer zone
SIDEBAR_RIGHT = 0.93  # right 7% = sidebar zone (for BMJ-style rotated text)

# Minimum pages with same text to be classified as a running header
RUNNING_HEADER_MIN_PAGES = 3


# ── Boilerplate page detection ────────────────────────────────────────────────

BOILERPLATE_SIGNATURES = [
    "Your use of the JSTOR archive indicates your acceptance",
    "JSTOR is a not-for-profit service",
    "collaborating with JSTOR to digitize",
]


def is_boilerplate_page(page: fitz.Page) -> bool:
    text = page.get_text()
    return any(sig in text for sig in BOILERPLATE_SIGNATURES)


# ── JSTOR metadata extraction (from cover page) ───────────────────────────────

def extract_jstor_metadata(page: fitz.Page) -> dict:
    """Parse the structured metadata block on a JSTOR cover page."""
    text = page.get_text()
    meta = {}

    title_match = re.match(r'^(.+?)\n', text.strip())
    if title_match:
        meta["title"] = title_match.group(1).strip()

    author_match = re.search(r'Author\(s\):\s*(.+)', text)
    if author_match:
        raw = author_match.group(1).strip()
        # "Charles S. Taber and Milton Lodge" → split on " and " or ","
        authors = re.split(r'\s+and\s+|,\s*', raw)
        meta["authors"] = [a.strip() for a in authors if a.strip()]

    source_match = re.search(r'Source:\s*(.+)', text)
    if source_match:
        meta["source"] = source_match.group(1).strip()

    year_match = re.search(r'\((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[\w.,\s]*(\d{4})\)', text)
    if year_match:
        meta["year"] = year_match.group(1)

    return meta


# ── PDF-embedded metadata ─────────────────────────────────────────────────────

def extract_pdf_metadata(doc: fitz.Document) -> dict:
    """Read title and author from the PDF's own metadata fields."""
    raw = doc.metadata or {}
    result = {}

    title = (raw.get("title") or "").strip()
    if len(title) > 5:
        result["title"] = title

    author = (raw.get("author") or "").strip()
    if len(author) > 1:
        result["authors"] = [a.strip() for a in re.split(r"[;,]", author) if a.strip()]

    return result


# ── Visual metadata extraction (font-size analysis) ───────────────────────────

def extract_visual_metadata(doc: fitz.Document, page_idx: int) -> dict:
    """Infer title, abstract, and keywords from the first content page layout."""
    page = doc[page_idx]
    page_h = page.rect.height
    page_w = page.rect.width
    full_text = page.get_text()

    # Collect all text spans in the content zone with their font sizes
    spans = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        bx0, by0, bx1, by1 = block["bbox"]
        if by0 / page_h < HEADER_FRAC:
            continue
        if by1 / page_h > FOOTER_FRAC:
            continue
        if bx0 / page_w > SIDEBAR_RIGHT:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span["text"].strip()
                if len(text) > 3:
                    spans.append({
                        "text": text,
                        "size": round(span["size"], 1),
                        "y0": span["origin"][1],
                    })

    if not spans:
        return {}

    # Title = largest font text, reconstructed across multiple lines
    max_size = max(s["size"] for s in spans)
    title_min_size = max_size * 0.80
    title_spans = sorted(
        [s for s in spans if s["size"] >= title_min_size],
        key=lambda s: s["y0"],
    )

    title_lines = []
    if title_spans:
        cur_y = title_spans[0]["y0"]
        cur_words = []
        for s in title_spans:
            if abs(s["y0"] - cur_y) < 8:
                cur_words.append(s["text"])
            else:
                title_lines.append(" ".join(cur_words))
                cur_words = [s["text"]]
                cur_y = s["y0"]
        title_lines.append(" ".join(cur_words))

    title = " ".join(title_lines).strip()

    # Abstract: find the labeled block
    abstract = ""
    abstract_match = re.search(
        r"(?:^|\n)\s*(?:Abstract|ABSTRACT)\s*\n(.*?)"
        r"(?=\n\s*(?:Keywords|Key\s?words|KEYWORDS|Introduction|INTRODUCTION"
        r"|\n[A-Z][A-Z\s]{4,}\n|$))",
        full_text,
        re.DOTALL | re.IGNORECASE,
    )
    if abstract_match:
        abstract = re.sub(r"\s+", " ", abstract_match.group(1)).strip()

    # Keywords: stop at blank line, digit-led line (affiliations like "1Michigan…"),
    # or a line that looks like a new section
    keywords = []
    kw_match = re.search(
        r"(?:Keywords|Key\s?words|KEYWORDS)[:\s]+(.*?)(?=\n\s*\n|\n\d|\n[A-Z][a-z]|\Z)",
        full_text,
        re.DOTALL,
    )
    if kw_match:
        raw_kw = kw_match.group(1).strip()
        # Take only the first line if it's a simple comma-separated list
        first_line = raw_kw.split("\n")[0].strip()
        keywords = [k.strip().rstrip(".") for k in re.split(r"[,;]", first_line) if k.strip()]

    return {"title": title, "abstract": abstract, "keywords": keywords}


def extract_metadata(doc: fitz.Document, content_pages: list, boilerplate_pages: list) -> dict:
    """Merge all metadata sources: JSTOR cover, PDF fields, visual analysis."""
    meta = {}

    # 1. JSTOR cover page (best structured metadata)
    for i in boilerplate_pages:
        page_text = doc[i].get_text()
        if "Author(s):" in page_text:
            meta.update(extract_jstor_metadata(doc[i]))
            break

    # 2. Visual extraction from first content page (done before PDF meta so we can compare)
    visual = {}
    if content_pages:
        visual = extract_visual_metadata(doc, content_pages[0])

    # 3. PDF embedded fields
    pdf_meta = extract_pdf_metadata(doc)

    # Title: prefer the longest/most complete version
    candidates = [
        meta.get("title", ""),
        pdf_meta.get("title", ""),
        visual.get("title", ""),
    ]
    best_title = max(candidates, key=len)
    if best_title:
        meta["title"] = best_title

    # Authors: JSTOR cover > PDF metadata > not extracted visually (too unreliable)
    if not meta.get("authors") and pdf_meta.get("authors"):
        # Strip "and " prefix that sometimes appears on the last author
        meta["authors"] = [
            re.sub(r"^and\s+", "", a, flags=re.IGNORECASE).strip()
            for a in pdf_meta["authors"]
        ]

    if visual.get("abstract"):
        meta["abstract"] = visual["abstract"]
    if visual.get("keywords"):
        meta["keywords"] = visual["keywords"]

    return meta


# ── Running header detection ──────────────────────────────────────────────────

def detect_running_headers(doc: fitz.Document, content_pages: list) -> set:
    """
    Text appearing in the header/footer zone on 3+ pages is a running header.
    Returns a set of normalised strings to filter out during cleaning.
    """
    if len(content_pages) < RUNNING_HEADER_MIN_PAGES:
        return set()

    occurrences = defaultdict(set)

    for page_idx in content_pages:
        page = doc[page_idx]
        page_h = page.rect.height
        page_w = page.rect.width

        for block in page.get_text("blocks"):
            if block[6] != 0:
                continue
            x0, y0, x1, y1, text = block[:5]
            text = text.strip()
            if len(text) < 3:
                continue

            in_header = (y0 / page_h) < HEADER_FRAC
            in_footer = (y1 / page_h) > FOOTER_FRAC
            in_sidebar = (x0 / page_w) > SIDEBAR_RIGHT

            if in_header or in_footer or in_sidebar:
                occurrences[text].add(page_idx)
                # Also index with leading/trailing page numbers stripped
                norm = re.sub(r"^\d+\s+|\s+\d+$", "", text).strip()
                if norm:
                    occurrences[norm].add(page_idx)

    return {t for t, pages in occurrences.items() if len(pages) >= RUNNING_HEADER_MIN_PAGES}


# ── Markdown cleaning ─────────────────────────────────────────────────────────

# Citation footer: "Miller G. Med Humanit 2025;51:218–227." (may contain **bold** around numbers)
# Allow optional spaces around ; and : since bold-stripping can leave extra whitespace
_CITATION_FOOTER = re.compile(r"^[A-Z][a-z]+ [A-Z]\.\s+\S.+\d{4}[;,]\s*\d+\s*:\s*\d+")

# Standalone DOI line
_DOI_LINE = re.compile(r"^doi:\s*10\.", re.IGNORECASE)

# Journal section labels that repeat as running content in some publishers
_SECTION_LABELS = {
    "original research", "research article", "article", "review article",
    "brief report", "case report", "letter",
}

# First-page metadata patterns to strip from body text
_FIRST_PAGE_META = re.compile(
    r"^("
    r"Correspondence\s+(to|author)"          # "Correspondence to Dr..."
    r"|Corresponding\s+Author:"             # "Corresponding Author:"
    r"|Accepted\s+\d"                        # "Accepted 30 May 2024"
    r"|Published\s+Online"                   # "Published Online First..."
    r"|To\s+cite:"                           # "To cite: ..."
    r"|©\s*(?:Author|The\s+Author|\d{4})"   # copyright lines
    r"|This\s+article\s+was\s+published\s+Online"
    r"|\d{1,2}Michigan\s+State|\d{1,2}[A-Z]"  # affiliation numbers "1Michigan..."
    r")",
    re.IGNORECASE,
)


def _plain(text: str) -> str:
    """Strip markdown formatting and normalise whitespace for comparison."""
    t = re.sub(r"\*+", "", text)   # remove bold/italic markers
    t = re.sub(r"_+", "", t)       # remove underscore emphasis
    t = re.sub(r"`+", "", t)       # remove code markers
    t = re.sub(r"\s+", " ", t)     # normalise whitespace (handles **51** → extra spaces)
    return t.strip()


# Paragraph-level patterns to remove entirely (multi-line blocks we can't filter line-by-line)
_PARA_SKIP = [
    re.compile(r"^this research was supported", re.IGNORECASE),
    re.compile(r"^this article (?:was|has been) published", re.IGNORECASE),
    re.compile(r"^correspondence concerning this article", re.IGNORECASE),
    re.compile(r"^this article was published online", re.IGNORECASE),
    # Long affiliation block: "Name, Institution; Name2, Institution2"
    re.compile(r"^[A-Z][a-z]+ [A-Z]\.? [A-Z][a-z]+,.+(?:University|Institute|College|Centre|Center).+;", re.IGNORECASE),
]

# Journal publication header: "Journal of X Year, Vol. N, No. N, pp–pp"
_PUB_HEADER = re.compile(
    r"^\S.{0,60}(?:Vol\.?\s*\d|No\.?\s*\d).{0,40}\d{4}|\d{4}.{0,20}(?:Vol\.?\s*\d|No\.?\s*\d)",
    re.IGNORECASE,
)


def _dedup_paragraphs(text: str) -> str:
    """Remove duplicate paragraphs (second occurrence of any paragraph >80 chars)."""
    paragraphs = re.split(r"\n{2,}", text)
    seen: set[str] = set()
    unique = []
    for para in paragraphs:
        # Skip paragraphs matching meta patterns
        plain = _plain(para)
        if any(p.search(plain) for p in _PARA_SKIP):
            continue
        key = plain
        if len(key) > 80 and key in seen:
            continue
        if len(key) > 80:
            seen.add(key)
        unique.append(para)
    return "\n\n".join(unique)


def clean_markdown(md: str, running_headers: set) -> str:
    lines = md.split("\n")
    out = []

    for line in lines:
        s = line.strip()
        plain_s = _plain(s)

        # Empty headings: "## " with nothing after
        if re.match(r"^#{1,6}\s*$", s):
            continue

        # Bare page numbers
        if re.match(r"^\d{1,4}$", plain_s):
            continue

        # Known journal section label banners
        if plain_s.lower() in _SECTION_LABELS:
            continue

        # Running headers — compare against plain text (no bold markers)
        skip = False
        for rh in running_headers:
            plain_rh = _plain(rh)
            if plain_s == plain_rh:
                skip = True
                break
            # Also try with leading/trailing page numbers stripped
            norm = re.sub(r"^\d+\s+|\s+\d+$", "", plain_s).strip()
            if norm and norm == plain_rh:
                skip = True
                break
        if skip:
            continue

        # Citation footer lines (strip bold markers before matching)
        if _CITATION_FOOTER.match(plain_s) and len(plain_s) < 150:
            continue

        # Standalone DOI lines
        if _DOI_LINE.match(plain_s):
            continue

        # First-page metadata patterns (affiliation, correspondence, copyright, "To cite:")
        if _FIRST_PAGE_META.match(plain_s):
            continue

        # Journal publication header line (short line with Vol./No. and year, no sentence structure)
        if _PUB_HEADER.match(plain_s) and len(plain_s) < 100 and plain_s.count(".") < 3:
            continue

        # Short ## headings that look like an author name ("## First Last" or "## First M. Last")
        # These appear when pymupdf4llm picks up the author byline as a heading
        if re.match(r"^#{1,2}\s+[A-Z][a-z]+(?:\s+[A-Z]\.)?\s+[A-Z][a-z]+\s*$", s):
            continue

        # Short lines that are just "Name Institution" (author + affiliation, no sentence)
        # e.g. "Ben M. Tappin Royal Holloway, University of London"
        # or "Leslie van der Leer Regent's University London"
        if (len(plain_s) < 85
                and re.search(r"\b(?:University|College|Institute|Holloway|Regent'?s)\b", plain_s, re.IGNORECASE)
                and not plain_s.endswith(".")):
            continue

        # Short standalone affiliation lines: "Department, University of X, City, Country"
        if (len(plain_s) < 90
                and re.match(r"^(?:School|Department|Faculty|Centre|Center|Division) of\b", plain_s, re.IGNORECASE)):
            continue

        out.append(line)

    result = "\n".join(out)
    # pymupdf4llm inserts "-----" between pages — collapse to blank line
    result = re.sub(r"\n-{4,}\n", "\n\n", result)
    # Collapse 4+ blank lines to 2
    result = re.sub(r"\n{4,}", "\n\n\n", result)
    # Remove duplicate paragraphs from page-boundary overlaps
    result = _dedup_paragraphs(result)

    return result.strip()


# ── YAML frontmatter builder ──────────────────────────────────────────────────

def build_frontmatter(meta: dict) -> str:
    lines = ["---"]

    if meta.get("title"):
        title = meta["title"].replace('"', '\\"')
        lines.append(f'title: "{title}"')

    if meta.get("authors"):
        authors_yaml = ", ".join(f'"{a}"' for a in meta["authors"])
        lines.append(f"authors: [{authors_yaml}]")

    if meta.get("year"):
        lines.append(f"year: {meta['year']}")

    if meta.get("source"):
        source = meta["source"].replace('"', '\\"')
        lines.append(f'source: "{source}"')

    if meta.get("keywords"):
        kw_yaml = ", ".join(f'"{k}"' for k in meta["keywords"][:10])
        lines.append(f"keywords: [{kw_yaml}]")

    lines.append("---")
    return "\n".join(lines)


# ── Core conversion ───────────────────────────────────────────────────────────

def convert_pdf(pdf_path: Path) -> str:
    doc = fitz.open(str(pdf_path))
    total = len(doc)

    boilerplate_pages = [i for i in range(total) if is_boilerplate_page(doc[i])]
    content_pages = [i for i in range(total) if i not in boilerplate_pages]

    if not content_pages:
        doc.close()
        return f"# {pdf_path.stem}\n\n*No content could be extracted.*\n"

    first_page = content_pages[0]
    ref_page = doc[first_page]
    page_h = ref_page.rect.height
    page_w = ref_page.rect.width

    # Margins to pass to pymupdf4llm (in PDF points)
    top_margin = page_h * HEADER_FRAC
    bottom_margin = page_h * (1 - FOOTER_FRAC)
    right_margin = page_w * (1 - SIDEBAR_RIGHT)
    left_margin = 0.0

    running_headers = detect_running_headers(doc, content_pages)
    meta = extract_metadata(doc, content_pages, boilerplate_pages)

    doc.close()

    # Convert with pymupdf4llm
    try:
        md_body = pymupdf4llm.to_markdown(
            str(pdf_path),
            pages=content_pages,
            margins=(left_margin, top_margin, right_margin, bottom_margin),
            show_progress=False,
        )
    except Exception as exc:
        # Fallback: plain text extraction
        print(f"    [warning] pymupdf4llm failed ({exc}), using plain text fallback")
        doc2 = fitz.open(str(pdf_path))
        md_body = "\n\n".join(doc2[i].get_text() for i in content_pages)
        doc2.close()

    md_body = clean_markdown(md_body, running_headers)

    # Refine title: if the first heading in the body is longer than what we extracted,
    # use it (catches cases where PDF metadata has a shortened title)
    first_heading_match = re.search(r"^#{1,2}\s+(.+)$", md_body, re.MULTILINE)
    if first_heading_match:
        candidate = _plain(first_heading_match.group(1))
        if len(candidate) > len(meta.get("title", "")):
            meta["title"] = candidate

    # Remove headings from the body that exactly duplicate the document title
    # (pymupdf4llm often repeats the title/author as ## headings on the first page)
    if meta.get("title"):
        title_plain = _plain(meta["title"])
        md_body = re.sub(
            r"^#{1,3}\s+" + re.escape(title_plain) + r"\s*$",
            "",
            md_body,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        md_body = re.sub(r"\n{3,}", "\n\n", md_body).strip()

    # Assemble final document
    parts = []

    if meta:
        parts.append(build_frontmatter(meta))
        parts.append("")

    if meta.get("title"):
        parts.append(f"# {meta['title']}")
        parts.append("")

    # Prepend abstract if it was extracted but isn't already in the body
    if meta.get("abstract") and "abstract" not in md_body[:800].lower():
        parts.append("## Abstract")
        parts.append("")
        parts.append(meta["abstract"])
        parts.append("")

    parts.append(md_body)

    return "\n".join(parts)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    INPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    # Accept optional file argument
    if len(sys.argv) > 1:
        pdfs = [Path(p) for p in sys.argv[1:]]
        missing = [p for p in pdfs if not p.exists()]
        if missing:
            print(f"File(s) not found: {', '.join(str(p) for p in missing)}")
            sys.exit(1)
    else:
        pdfs = sorted(INPUT_DIR.glob("*.pdf"))

    if not pdfs:
        print("No PDFs found in input/. Drop PDF files there and run again.")
        sys.exit(0)

    print(f"Converting {len(pdfs)} PDF(s)…\n")
    ok = 0

    for pdf_path in pdfs:
        # Output goes to output/ regardless of where the input came from
        out_path = OUTPUT_DIR / (pdf_path.stem + ".md")
        print(f"  {pdf_path.name}")
        try:
            md = convert_pdf(pdf_path)
            out_path.write_text(md, encoding="utf-8")
            print(f"  -> {out_path}\n")
            ok += 1
        except Exception as exc:
            print(f"  ERROR: {exc}\n")

    print(f"Done: {ok}/{len(pdfs)} converted. Output in {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
