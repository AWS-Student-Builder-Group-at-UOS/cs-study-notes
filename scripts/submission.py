from html.parser import HTMLParser
from pathlib import Path
import re
import unicodedata

from pypdf import PdfReader

COMMENTS = re.compile(r"<!--.*?(?:-->|$)", flags=re.DOTALL)


def meaningful_text(text: str) -> bool:
    text = COMMENTS.sub("", text)
    return any(not char.isspace() and not unicodedata.category(char).startswith("C")
               for char in text)


class VisibleTextParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}
    HIDDEN_TAGS = {"head", "script", "style", "template", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.has_text = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = (bool(self.stack and self.stack[-1][1]) or tag in self.HIDDEN_TAGS
                  or "hidden" in attrs
                  or (attrs.get("aria-hidden") or "").strip().lower() == "true")
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, hidden))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not (self.stack and self.stack[-1][1]) and meaningful_text(data):
            self.has_text = True


def has_content(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in {".md", ".txt", ".html", ".pdf"} or path.is_symlink():
        return False
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        if suffix == ".pdf":
            with path.open("rb") as stream:
                reader = PdfReader(stream, strict=True)
                return not reader.is_encrypted and any(
                    meaningful_text(page.extract_text() or "") for page in reader.pages)
        text = path.read_text(encoding="utf-8-sig")
        if suffix == ".html":
            parser = VisibleTextParser()
            parser.feed(COMMENTS.sub("", text))
            parser.close()
            return parser.has_text
        return meaningful_text(text)
    except Exception:
        return False
