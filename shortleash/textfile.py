"""Decode a text file the way a Windows tool may have written it.

One decoder for every file the user hands to shortleash: the prompt file of
`cache check|make` and the `--pricing` / $SHORTLEASH_PRICING JSON file. What
PowerShell 5 and Notepad write is not plain UTF-8: `>` and `Out-File`
produce UTF-16 LE with a byte-order mark, `Set-Content -Encoding utf8` and
Notepad put a mark in front of UTF-8. Python's strict "utf-8" codec reads that
mark as a stray character (json refuses it) and cannot read UTF-16 at all, so a
file the user cannot tell from a good one would be refused.
"""

from __future__ import annotations

import codecs

# The byte-order marks a file may start with. The UTF-32 LE mark begins with the
# UTF-16 LE mark, so the UTF-32 ones are tried first. The "utf-8-sig", "utf-16"
# and "utf-32" codecs consume the mark: it is not part of the text.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32", "UTF-32"),
    (codecs.BOM_UTF32_BE, "utf-32", "UTF-32"),
    (codecs.BOM_UTF8, "utf-8-sig", "UTF-8"),
    (codecs.BOM_UTF16_LE, "utf-16", "UTF-16"),
    (codecs.BOM_UTF16_BE, "utf-16", "UTF-16"),
)

_EXPECTED = ("expected UTF-8, or UTF-16/UTF-32 with a byte-order mark - "
             "re-save the file as UTF-8")


def decode_text(data: bytes) -> str:
    """Decode the bytes of a text file.

    UTF-8 (with or without a byte-order mark) is read, and so are UTF-16 and
    UTF-32 files that start with a mark. Line endings are left as they are.
    Anything else raises ValueError, whose message completes the sentence
    "<file> ..." and says what was expected: a file in some other encoding
    would otherwise be read, and rewritten, as garbage."""
    codec, label = "utf-8", "UTF-8"
    for mark, mark_codec, mark_label in _BOMS:
        if data.startswith(mark):
            codec, label = mark_codec, mark_label
            break
    try:
        text = data.decode(codec)
    except UnicodeDecodeError as e:
        raise ValueError(f"is not valid {label} text ({e.reason} at byte "
                         f"{e.start}); {_EXPECTED}") from None
    if "\x00" in text:
        # UTF-16 without a mark: every other byte is NUL, which is valid UTF-8
        raise ValueError("contains NUL characters (UTF-16 without a "
                         f"byte-order mark?); {_EXPECTED}")
    return text
