"""Build the PoC knowledge base from the vendor KB PDFs (pseudonymised).

Normally run through ../setup_kb.sh (interactive). Direct use (needs pypdf):

    python3 build_kb_index.py --kb-dir "<doc dir>" [--map "Folder=CODE"] [--out DIR]
        -> DIR/kb_index.json (local BM25 mode) and DIR/kb_gaps.json
    python3 build_kb_index.py --kb-dir ... --export-text DIR --manifest manifest.json
        -> one pseudonymised .txt per PDF + manifest (Bedrock Knowledge Base mode)

All text, file names and chunk ids pass through tools/anonymize.py with the map at
<PoC>/../anonymize_map.json (or env ANONYMIZE_MAP). The map also provides the folder -> hotel
code mapping ("folders") and extra files to skip ("skip_files"). Without a map nothing is
pseudonymised and unknown folders get code GENERAL (setup_kb.sh asks before continuing).
"""
import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import anonymize  # noqa: E402

POC_DIR = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_KB_DIR = POC_DIR.parent / "00_Vendor KB (pdf)"
DEFAULT_OUT = POC_DIR / "lambda" / "data"

FOLDER_TO_CODE = {"General Info": "GENERAL", "Booking Related": "BOOKING"}  # the rest comes from the map
# Process documents already encoded in the prompt / flow; keep them out of retrieval.
SKIP_FILES = {"Email AI POC Related Flow.pdf", "General Logic Requirement to AI.pdf"}
MAX_CHARS = 1800
MIN_CHARS = 40


def folder_codes(mapping=None):
    mapping = mapping if mapping is not None else anonymize.load()
    return {**FOLDER_TO_CODE, **((mapping or {}).get("folders") or {})}


def category_of(path):
    parts = re.split(r"\s[-–]\s", path.stem, maxsplit=1)
    return (parts[1] if len(parts) > 1 else path.stem).strip()


def clean(text):
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_long(text):
    if len(text) <= MAX_CHARS:
        return [text]
    out, buf = [], ""
    for para in text.split("\n"):
        if len(buf) + len(para) + 1 > MAX_CHARS and buf:
            out.append(buf)
            buf = buf[-200:]  # small overlap keeps table rows together
        buf = f"{buf}\n{para}" if buf else para
    if buf:
        out.append(buf)
    return out


def documents(kb_dir, codes, skip):
    """Yield (path, hotel_code, category, source) - category/source already pseudonymised."""
    for pdf in sorted(kb_dir.rglob("*.pdf")):
        if pdf.name in skip:
            continue
        rel = pdf.relative_to(kb_dir)
        top = rel.parts[0] if len(rel.parts) > 1 else ""
        code = codes.get(top, "GENERAL")
        tail = pathlib.PurePosixPath(*rel.parts[1:]) if len(rel.parts) > 1 else pathlib.PurePosixPath(rel.name)
        yield pdf, code, category_of(pdf), f"{code}/{tail.as_posix()}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb-dir", default=str(DEFAULT_KB_DIR))
    ap.add_argument("--map", action="append", default=[], help='extra "Folder name=CODE" mapping')
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--manifest")
    ap.add_argument("--export-text", help="write one pseudonymised .txt per PDF into this directory (Bedrock mode)")
    args = ap.parse_args()

    kb_dir = pathlib.Path(args.kb_dir).expanduser().resolve()
    if not kb_dir.is_dir():
        sys.exit(f"知识库目录不存在：{kb_dir}")
    mapping = anonymize.load()
    codes = folder_codes(mapping)
    for m in args.map:
        folder, _, code = m.partition("=")
        codes[folder.strip()] = code.strip().upper() or "GENERAL"
    skip = SKIP_FILES | set((mapping or {}).get("skip_files") or [])
    anon = lambda t: anonymize.apply(t, mapping, normalize=True)  # noqa: E731
    docs = [(p, c, anon(cat), anon(src)) for p, c, cat, src in documents(kb_dir, codes, skip)]
    if not docs:
        sys.exit(f"目录中没有 PDF：{kb_dir}")
    print(f"pseudonymisation map: {anonymize.map_path() if mapping else 'NOT FOUND - real names kept'}", file=sys.stderr)

    import pypdf

    chunks, gaps, exported = [], [], []
    for pdf, code, category, source in docs:
        try:
            reader = pypdf.PdfReader(str(pdf))
        except Exception as exc:  # corrupted PDF
            gaps.append({"source": source, "hotel_code": code, "reason": f"unreadable: {type(exc).__name__}"})
            continue
        doc_pages = []
        for page_no, page in enumerate(reader.pages, start=1):
            text = anon(clean(page.extract_text() or ""))
            if len(text) < MIN_CHARS:
                continue
            doc_pages.append((page_no, text))
            for part_no, part in enumerate(split_long(text), start=1):
                chunks.append({"id": f"{code}:{pathlib.PurePosixPath(source).stem}:p{page_no}" + (f".{part_no}" if part_no > 1 else ""),
                               "hotel_code": code, "category": category, "source": source, "page": page_no, "text": part})
        if not doc_pages:
            gaps.append({"source": source, "hotel_code": code, "reason": "no extractable text (image-only PDF, needs OCR)"})
        elif args.export_text:
            dst = pathlib.Path(args.export_text) / (source[:-4] + ".txt")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text("\n\n".join(f"[Page {n}]\n{t}" for n, t in doc_pages), encoding="utf-8")
            exported.append({"path": str(dst), "hotel_code": code, "category": category, "source": source})

    if args.manifest:
        pathlib.Path(args.manifest).write_text(json.dumps(exported, ensure_ascii=False), encoding="utf-8")
    if args.export_text:
        print(f"exported {len(exported)} text documents, {len(gaps)} without text", file=sys.stderr)
        return
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "kb_index.json").write_text(json.dumps(chunks, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out / "kb_gaps.json").write_text(json.dumps(gaps, ensure_ascii=False, indent=1), encoding="utf-8")
    by_code = {}
    for c in chunks:
        by_code[c["hotel_code"]] = by_code.get(c["hotel_code"], 0) + 1
    print(f"chunks={len(chunks)} by_code={by_code} gaps={len(gaps)}", file=sys.stderr)


if __name__ == "__main__":
    main()
