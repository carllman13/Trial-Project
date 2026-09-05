"""HTML -> plain text, and whitespace normalisation.

Standard library only, deliberately: corporate machines often block pip.
"""
import re
from html.parser import HTMLParser

# Tags whose content is not text.
_SKIP = {"script", "style", "head", "title"}
# Tags that imply a line break when they open or close.
_BLOCK = {
    "p", "div", "br", "tr", "li", "table", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "pre",
}

# Characters that look like nothing but break literal matching.
_INVISIBLE = re.compile(r"[​‌‍⁠﻿­]")
# Unicode spaces that are not plain 0x20.
_ODD_SPACE = re.compile(r"[      ]")


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    p = _Extractor()
    p.feed(html)
    p.close()
    return normalize("".join(p.parts))


def normalize(text: str) -> str:
    """Make text safe to match patterns against.

    Removes zero-width characters and folds exotic spaces to plain spaces --
    both are invisible on screen and both silently break literal matching.
    Collapses runs of blank lines so output stays readable.
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


def quoted_history(body_text: str, unique_text: str) -> str:
    """Whatever the full body had that the unique body did not.

    uniqueBody strips quoted chain by shape, so this is where the history of a
    thread you were added to late ends up. Best effort: if the unique text is
    not a clean prefix of the body, keep the whole body rather than guess.
    """
    body_text = normalize(body_text)
    unique_text = normalize(unique_text)
    if not unique_text:
        return body_text
    idx = body_text.find(unique_text)
    if idx == -1:
        return body_text
    return normalize(body_text[:idx] + body_text[idx + len(unique_text):])
