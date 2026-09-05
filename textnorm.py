"""HTML -> plain text, and the whitespace normalisation everything else relies on.

Standard library only, deliberately: pip is often blocked on a work machine.
"""
import re
from html.parser import HTMLParser

# Zero-width and soft-hyphen characters. Invisible on screen, and they sit
# inside words where they silently break literal matching.
_INVISIBLE = re.compile(r"[​‌‍⁠﻿­]")
# Spaces that are not U+0020. Outlook emits non-breaking spaces constantly.
_ODD_SPACE = re.compile(r"[   -   　]")

_SKIP_TAGS = {"script", "style", "head", "title"}
_BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "table", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "pre",
}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)


def normalize(text):
    """Make text safe to match patterns against.

    Folds exotic spaces to plain ones and drops zero-width characters: both
    are invisible when you read the email, and both stop a hand-typed pattern
    from matching. Collapses runs of blank lines so stored text stays readable.
    """
    if not text:
        return ""
    text = _INVISIBLE.sub("", text)
    text = _ODD_SPACE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def collapse(text):
    """All whitespace to single spaces. Used when matching one logical line."""
    return re.sub(r"\s+", " ", text or "").strip()


def html_to_text(html):
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return normalize("".join(parser.parts))


def to_text(body, body_type):
    """Body of either type to normalised plain text."""
    if (body_type or "html").lower() == "html":
        return html_to_text(body)
    return normalize(body)
