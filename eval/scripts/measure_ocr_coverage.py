"""What OCR adds per page, so enabling it is a decision, not a guess.

The operational manuals carry most of their content inside screenshots: one
reference manual has 21 pages, ~7.100 chars of native text and 48 embedded
images. With DOCLING_ENABLED=false such a document indexes silently *half* —
the "no text extracted" warning never fires because the document is not
empty, only incomplete. This script converts one PDF twice through the same
Docling configuration production uses (RapidOCR, torch backend, the language
set from DOCLING_OCR_LANGS — "en" selects PP-OCRv6, whose character set
covers Portuguese diacritics), varying only do_ocr, and reports what OCR
recovered per page.

The accented samples exist for eyeball inspection: PP-OCRv6 covering PT
diacritics is a claim in a comment (config.py), and this is where it gets
checked against a real corpus before anyone trusts it.

Usage:
    python eval/scripts/measure_ocr_coverage.py <pdf>
    make measure-ocr PDF=<pdf>

Needs the Docling models (prefetched in the Docker image, downloaded on
first use elsewhere). No Qdrant, no OpenAI.
"""

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from docquery.config import Settings  # noqa: E402
from docquery.ingest import docling_loader  # noqa: E402

_ACCENTED = re.compile(r"[áàâãéêíóôõúüçÁÀÂÃÉÊÍÓÔÕÚÜÇ]")


def _page_lines(dl_doc) -> dict[int, list[str]]:
    """Text items grouped by the first page each one appears on."""
    pages: dict[int, list[str]] = {}
    for item in getattr(dl_doc, "texts", []) or []:
        text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue
        for prov in getattr(item, "prov", []) or []:
            page = getattr(prov, "page_no", None)
            if page:
                pages.setdefault(int(page), []).append(text)
                break
    return pages


def measure(pdf: Path, ocr: bool) -> dict[int, list[str]]:
    settings = Settings(docling_enabled=True, docling_ocr_enabled=ocr)
    dl_doc = docling_loader.convert(pdf, settings)
    return _page_lines(dl_doc)


def _stats(lines: list[str]) -> dict:
    text = " ".join(lines)
    return {
        "chars": len(text),
        "words": len(text.split()),
        "accented_chars": len(_ACCENTED.findall(text)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("eval/results/ocr_coverage")
    )
    parser.add_argument("--samples", type=int, default=10)
    args = parser.parse_args()

    without = measure(args.pdf, ocr=False)
    with_ocr = measure(args.pdf, ocr=True)

    pages = sorted(set(without) | set(with_ocr))
    rows = []
    ocr_only_accented: list[tuple[int, str]] = []
    print(f"{'page':>4}  {'chars off':>9}  {'chars on':>9}  {'delta':>7}  accented on")
    for page in pages:
        off = _stats(without.get(page, []))
        on = _stats(with_ocr.get(page, []))
        delta = on["chars"] - off["chars"]
        print(
            f"{page:>4}  {off['chars']:>9}  {on['chars']:>9}  {delta:>+7}"
            f"  {on['accented_chars']:>4}"
        )
        rows.append({"page": page, "ocr_off": off, "ocr_on": on, "delta_chars": delta})
        native = set(without.get(page, []))
        for line in with_ocr.get(page, []):
            if line not in native and _ACCENTED.search(line):
                ocr_only_accented.append((page, line))

    print(f"\nAccented lines that exist ONLY with OCR ({len(ocr_only_accented)}):")
    for page, line in ocr_only_accented[: args.samples]:
        print(f"  p{page}: {line}")

    args.output.mkdir(parents=True, exist_ok=True)
    summary = args.output / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "pdf": str(args.pdf),
                "ocr_langs": Settings(docling_enabled=True).docling_ocr_langs,
                "pages": rows,
                "ocr_only_accented_lines": [
                    {"page": p, "text": t} for p, t in ocr_only_accented
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"\nWrote {summary}")


if __name__ == "__main__":
    main()
