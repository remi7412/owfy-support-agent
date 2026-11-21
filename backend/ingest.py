import io
import re
from html.parser import HTMLParser
from typing import Optional, Tuple
import httpx
from pypdf import PdfReader


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.in_title = False
        self.ignore_tags = {
            "script", "style", "noscript", "svg", "header", "footer",
            "nav", "aside", "iframe", "button"
        }
        self.current_ignored = 0
        self.text_chunks = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "title":
            self.in_title = True
        if t in self.ignore_tags:
            self.current_ignored += 1

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "title":
            self.in_title = False
        if t in self.ignore_tags and self.current_ignored > 0:
            self.current_ignored -= 1

    def handle_data(self, data):
        if self.in_title and not self.title:
            self.title = data.strip()
        if self.current_ignored == 0:
            cleaned = data.strip()
            if cleaned:
                self.text_chunks.append(cleaned)


def clean_extracted_text(raw_text: str) -> str:
    """Collapses repetitive spaces, tabs, and line breaks into clean prose."""
    text = re.sub(r"[ \t]+", " ", raw_text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def fetch_website_content(url: str, timeout: float = 15.0) -> Tuple[str, str]:
    """
    Fetches a webpage using httpx and extracts clean text and document title
    using Python's stdlib HTMLParser.
    """
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 OwfyAgent/1.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            html_content = resp.text
    except Exception as exc:
        raise ValueError(f"Failed to fetch URL ({exc})")

    parser = TextExtractor()
    parser.feed(html_content)

    title = parser.title.strip()
    if not title:
        host_segment = url.split("//")[-1].split("/")[0]
        title = f"Web: {host_segment}"

    body_text = "\n".join(parser.text_chunks)
    cleaned = clean_extracted_text(body_text)

    if len(cleaned) < 20:
        raise ValueError("Could not extract readable text from the provided webpage.")

    return title, cleaned


def extract_pdf_content(pdf_bytes: bytes, filename: Optional[str] = None) -> Tuple[str, str, int]:
    """
    Extracts text from PDF bytes using pypdf.
    Returns (title, full_text, page_count).
    """
    try:
        stream = io.BytesIO(pdf_bytes)
        reader = PdfReader(stream)
    except Exception as exc:
        raise ValueError(f"Invalid or corrupted PDF file: {exc}")

    num_pages = len(reader.pages)
    if num_pages == 0:
        raise ValueError("PDF contains no pages.")

    title = ""
    try:
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title).strip()
    except Exception:
        title = ""

    if not title and filename:
        title = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE).strip()
    if not title:
        title = "Uploaded PDF Document"

    extracted_pages = []
    for idx, page in enumerate(reader.pages):
        page_text = page.extract_text() or ""
        trimmed = page_text.strip()
        if trimmed:
            extracted_pages.append(f"--- Page {idx + 1} ---\n{trimmed}")

    combined = "\n\n".join(extracted_pages)
    cleaned = clean_extracted_text(combined)

    if len(cleaned) < 15:
        raise ValueError("No extractable text found in this PDF (it may contain only scanned images).")

    return title, cleaned, num_pages
