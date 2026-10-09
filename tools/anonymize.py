"""Pseudonymisation used when building the knowledge base.

The rules (regex -> replacement) live in a JSON map that is deliberately kept OUTSIDE PoC/
(default: <PoC>/../anonymize_map.json, override with env ANONYMIZE_MAP), because it lists the
real names it replaces. Format:

    {"folders": {"<document folder name>": "<hotel code>"},
     "skip_files": ["<pdf file name>", ...],
     "rules": [["<regex>", "<replacement>", "<flags>"], ...]}   flags: "i" ignore case, "k" knowledge base only

Rules are applied in order; put specific names before generic ones.
"""
import json
import os
import pathlib
import re
import unicodedata

DEFAULT_MAP = pathlib.Path(__file__).resolve().parents[2] / "anonymize_map.json"


def map_path():
    return pathlib.Path(os.environ.get("ANONYMIZE_MAP") or DEFAULT_MAP)


def load(path=None):
    path = pathlib.Path(path or map_path())
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_compiled"] = [(re.compile(p, re.I if "i" in (f or "") else 0), r, "k" in (f or "")) for p, r, f in data.get("rules", [])]
    return data


def apply(text, mapping, kb=True, normalize=False):
    """kb=True: all rules; kb=False: skip rules flagged 'k' (e.g. phone numbers).
    normalize=True (PDF text only): NFKC-fold CJK radical / compatibility code points (e.g. U+2F79 for the
    character used in brand names) so the rules match. Not used for hand-written files: it would also
    turn Chinese full-width punctuation into ASCII."""
    if not mapping or not text:
        return text
    if normalize:
        text = unicodedata.normalize("NFKC", text)
    for rx, repl, kb_only in mapping["_compiled"]:
        if kb or not kb_only:
            text = rx.sub(repl, text)
    return text
