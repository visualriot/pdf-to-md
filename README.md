# PDF to Markdown Converter

A Python-based PDF to Markdown converter designed for academic articles and journal papers.

The script converts PDFs into clean `.md` files with YAML frontmatter, extracted metadata, headings, abstract, keywords, and cleaned article body text.

It removes common PDF noise such as page numbers, running headers, footers, JSTOR boilerplate pages, DOI footer lines, author affiliation clutter, and repeated page-boundary text.

---

## Features

- Convert a single PDF or batch-convert all PDFs in `input/`
- Output clean Markdown files to `output/`
- Extract YAML frontmatter where possible:
  - title
  - authors
  - year
  - source
  - keywords
- Detect and remove JSTOR boilerplate cover pages
- Extract JSTOR metadata from cover pages
- Extract embedded PDF metadata
- Infer title, abstract, and keywords from the first content page
- Remove common academic publishing noise:
  - headers
  - footers
  - page numbers
  - running headers
  - citation footers
  - DOI-only lines
  - journal labels
  - author affiliation blocks
- Uses `pymupdf4llm` for layout-aware Markdown conversion
- Falls back to plain text extraction if Markdown conversion fails

---

## Requirements

- Python 3.9+
- PyMuPDF
- PyMuPDF4LLM

Install dependencies:

    pip install pymupdf pymupdf4llm

---

## Project Structure

    pdf-to-markdown/
    ├── convert.py
    ├── input/
    ├── output/
    └── README.md

Place PDF files inside the `input/` folder.

Converted Markdown files will be saved inside the `output/` folder.

---

## Usage

Convert all PDFs in the `input/` folder:

    python convert.py

Convert one specific PDF:

    python convert.py path/to/file.pdf

Convert multiple specific PDFs:

    python convert.py file1.pdf file2.pdf file3.pdf

---

## Output

Each converted file is saved in `output/` using the same filename as the original PDF, but with a `.md` extension.

Example:

    input/article.pdf

becomes:

    output/article.md

The output Markdown may include YAML frontmatter:

    ---
    title: "Example Article Title"
    authors: ["Author One", "Author Two"]
    year: 2024
    source: "Journal Name"
    keywords: ["keyword one", "keyword two"]
    ---

followed by the article content:

    # Example Article Title

    ## Abstract

    Abstract text here...

    ## Introduction

    Article body text here...

---

## Notes

This tool is mainly designed for academic PDFs, especially journal articles.

It works best with text-based PDFs. Scanned PDFs or image-only PDFs are not currently supported unless OCR is added separately.

Metadata extraction is heuristic, so results may vary depending on the publisher, PDF structure, and layout.

---

## Recommended `.gitignore`

The `input/` and `output/` folders may contain copyrighted PDFs or generated files, so they should usually not be committed.

Add this to `.gitignore`:

    input/
    output/
    *.pdf
    __pycache__/
    *.pyc
    .venv/
    .env

---

## Limitations

- Does not perform OCR on scanned documents
- Complex multi-column layouts may still need manual cleanup
- Tables may not always convert perfectly
- Author extraction from visual layout is intentionally avoided because it is unreliable
- Some publisher-specific metadata may still appear in the output

---

## Possible Future Improvements

- Add a simple Streamlit interface
- Add OCR support for scanned PDFs
- Add batch progress reporting
- Add table cleanup
- Add command-line flags for:
  - page ranges
  - keeping or removing frontmatter
  - preserving page breaks
  - exporting images
- Add unit tests with sample PDFs

---

## License

MIT License
