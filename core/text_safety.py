"""User-text hygiene shared by the API and the render layer (SECURITY S1c).

``clean_user_text`` removes control characters: whitespace-like controls
(TAB, LF, CR, VT, FF, NEL, LINE/PARAGRAPH SEPARATOR) become one space each,
every other C0/C1 control and DEL is dropped. Leading/trailing whitespace
is stripped.

``validated_topic`` is the API contract for a job topic: a string that is
1..``TOPIC_MAX_CHARS`` code points after cleaning. Error messages are fixed
strings and never echo the input.
"""
from __future__ import annotations

TOPIC_MAX_CHARS = 200

_WHITESPACE_CONTROLS = frozenset("\t\n\r\x0b\x0c\x85\u2028\u2029")


def clean_user_text(value: str) -> str:
    out = []
    for ch in value:
        if ch in _WHITESPACE_CONTROLS:
            out.append(" ")
            continue
        code = ord(ch)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            continue
        out.append(ch)
    return "".join(out).strip()


def validated_topic(value: object) -> str:
    """Return the cleaned topic or raise ValueError with a fixed message."""
    if not isinstance(value, str):
        raise ValueError("topic must be a string")
    topic = clean_user_text(value)
    if not 1 <= len(topic) <= TOPIC_MAX_CHARS:
        raise ValueError(f"topic must be 1-{TOPIC_MAX_CHARS} characters")
    return topic
