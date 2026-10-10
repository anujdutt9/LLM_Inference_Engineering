"""Cut each company's 10-K to ~10K tokens with citation anchors (Tenant B's input).

Sized for the agent harness as it is: tinyharness's Read returns up to 50,000 bytes per call
and cuts lines over 2,000 characters, and any message over 16K tokens is trimmed. So each
filing is < 48 KB, prose is wrapped at ~400 characters, and an analyst reads its whole
filing with one built-in Read. Every company gets the same cut (Qwen tokens):
  Item 1   Business               first ~1,000   (what the company does, segments)
  Item 1A  Risk Factors           first ~2,400   (most filings open with a risk summary)
  Item 7   MD&A                   first ~3,900   (overview, results, segments, liquidity)
  Item 8   Financial statements   ~800 each      (operations/income, balance sheet, cash flows)
Each kept passage is tagged [ticker-section-pNNN], so findings can cite it and grade.py can
check the citation.

    python -m app.agents.prep_10k            # datasets/exp1b/filings/raw/*.txt → filings/
"""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILINGS = ROOT / "datasets" / "exp1b" / "filings"

BUDGET = {"item1": 1000, "item1a": 2400, "item7": 3900, "statement": 800}
CHUNK_TOKENS = 300  # one anchor per ~350 tokens of text


@dataclass
class Company:
    ticker: str
    name: str
    fiscal_year: str
    period_end: str
    raw: str
    source: str
    # regexes (matched against a stripped line) for where each section starts
    item1: str
    item1a: str
    item7: str
    # where the section after each one starts (its end)
    ends: dict[str, str]


ITEM_END = {
    "item1": r"^item\s*1a\.?\s+risk factors$",
    "item1a": r"^item\s*1b\.?\s+unresolved staff comments$",
    "item7": r"^item\s*7a\.?\s+quantitative and qualitative disclosures? about market risk$",
}

COMPANIES = [
    Company("nvda", "NVIDIA Corporation", "fiscal 2026", "January 25, 2026", "nvda_10k_fy2026.txt",
            "https://d18rn0p25nwr6d.cloudfront.net/CIK-0001045810/e361e58a-7483-44f5-bc62-a9080ae6ec72.pdf (linked from investor.nvidia.com)",
            r"^item\s*1\.\s+business$", r"^item\s*1a\.\s+risk factors$",
            r"^item\s*7\.\s+management.s discussion and analysis of financial condition and results of operations$", ITEM_END),
    Company("amd", "Advanced Micro Devices, Inc.", "fiscal 2025", "December 27, 2025", "amd_10k_fy2025.txt",
            "https://ir.amd.com/financial-information/sec-filings/content/0000002488-26-000018/0000002488-26-000018.pdf",
            r"^item\s*1\.\s+business$", r"^item\s*1a\.\s+risk factors$",
            r"^item\s*7\.\s+management.s discussion and analysis of financial condition and results of operations$", ITEM_END),
    Company("intc", "Intel Corporation", "fiscal 2025", "December 27, 2025", "intc_10k_fy2025.txt",
            "https://www.intc.com/filings-reports/annual-reports/content/0000050863-26-000011/0000050863-26-000011.pdf",
            # Intel's 10-K uses its own layout: titled sections instead of "Item N." headings
            r"^our business$", r"^risk factors$", r"^management.s discussion and analysis$",
            {"item1": r"^management.s discussion and analysis$", "item1a": r"^(proxy statement|other key information|properties)$",
             "item7": r"^quantitative and qualitative disclosures about market risk$"}),
    Company("avgo", "Broadcom Inc.", "fiscal 2025", "November 2, 2025", "avgo_10k_fy2025.txt",
            "https://investors.broadcom.com/static-files/564b6a94-571d-4a43-9c3b-57cbd9477092",
            r"^item\s*1\.\s+business$", r"^item\s*1a\.\s+risk factors$",
            r"^item\s*7\.\s+management.s discussion and analysis of financial condition and results of operations$", ITEM_END),
]

STATEMENTS = [
    ("income", r"^consolidated statements? of (income|operations)$"),
    ("balance", r"^consolidated balance sheets?$"),
    ("cashflow", r"^consolidated statements? of cash flows$"),
]

# page furniture pdftotext leaves in the text
NOISE = re.compile(
    r"^(table of contents|\d{1,3}|.*form 10-k.*\|.*|.*\|\s*\d{1,3}\s*$|our business|md&a|risk factors|financial statements)$",
    re.IGNORECASE,
)


class Counter:
    def __init__(self) -> None:
        try:
            from tokenizers import Tokenizer

            self.tok = Tokenizer.from_pretrained("Qwen/Qwen3.5-9B")
        except Exception:
            self.tok = None

    def __call__(self, text: str) -> int:
        if self.tok is None:
            return max(1, len(text) // 4)
        return len(self.tok.encode(text, add_special_tokens=False).ids)


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.replace("’", "'")).strip().lower()


def find(lines: list[str], pattern: str, start: int = 0) -> int | None:
    """First line at or after `start` matching `pattern` as a whole heading (table-of-
    contents lines carry a page number and so don't match)."""
    rx = re.compile(pattern)
    for i in range(start, len(lines)):
        if rx.match(_norm(lines[i])):
            return i
    return None


def paragraphs(lines: list[str], table: bool = False) -> list[str]:
    """Join wrapped lines into paragraphs; tables keep one row per line, columns as ' | '."""
    out: list[str] = []
    cur: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if cur:
                out.append(("\n" if table else " ").join(cur))
                cur = []
            continue
        if NOISE.match(line.strip()) and not table:
            continue
        if table:
            cells = [c.strip() for c in re.split(r"\s{2,}", line.strip()) if c.strip()]
            cur.append(" | ".join(cells))
        else:
            cur.append(re.sub(r"\s+", " ", line.strip()))
    if cur:
        out.append(("\n" if table else " ").join(cur))
    return [p for p in out if len(p) > 1 and not _is_nav(p)]


def _is_nav(paragraph: str) -> bool:
    """Intel's pages carry a navigation strip ("Operating Segment Results 21 Consolidated
    Results of Operations 25 ..."): titles followed by page numbers, not prose."""
    if len(paragraph) < 60 and re.fullmatch(r"[A-Za-z&' ]+ \d{1,3}", paragraph):
        return True
    return len(re.findall(r"[A-Za-z&)] \d{1,3}(?= [A-Z]|$)", paragraph)) >= 3


def take(paras: list[str], budget: int, count: Counter) -> list[str]:
    kept, used = [], 0
    for p in paras:
        n = count(p)
        if used + n > budget:
            if "\n" in p:  # a table: keep the rows that fit
                rows: list[str] = []
                for row in p.split("\n"):
                    used += count(row)
                    if used > budget:
                        break
                    rows.append(row)
                if rows:
                    kept.append("\n".join(rows))
            elif not kept:  # one huge paragraph: cut it
                kept.append(p[: budget * 4])
            break
        kept.append(p)
        used += n
    return kept


def anchored(ticker: str, section: str, paras: list[str], count: Counter) -> list[tuple[str, str]]:
    """Group paragraphs into ~CHUNK_TOKENS passages, each with its own anchor."""
    chunks: list[tuple[str, str]] = []
    cur: list[str] = []
    size = 0
    for p in paras:
        cur.append(p)
        size += count(p)
        if size >= CHUNK_TOKENS:
            chunks.append((f"{ticker}-{section}-p{len(chunks) + 1:03d}", "\n\n".join(cur)))
            cur, size = [], 0
    if cur:
        chunks.append((f"{ticker}-{section}-p{len(chunks) + 1:03d}", "\n\n".join(cur)))
    return chunks


def _wrap(text: str, width: int = 400) -> str:
    """Prose wrapped at ~400 characters (table rows are short already)."""
    return "\n".join(
        line if len(line) <= width else textwrap.fill(line, width, break_long_words=False, break_on_hyphens=False)
        for line in text.split("\n")
    )


def section_lines(lines: list[str], c: Company, key: str) -> list[str]:
    start = find(lines, getattr(c, key), 60)
    if start is None:
        raise SystemExit(f"{c.ticker}: no {key} heading ({getattr(c, key)})")
    end = find(lines, c.ends[key], start + 1) or len(lines)
    return lines[start + 1 : end]


def statement_lines(lines: list[str], pattern: str) -> list[str]:
    """The statement table itself: the first matching heading followed by numbers."""
    i = 0
    while (start := find(lines, pattern, i)) is not None:
        body = lines[start + 1 : start + 120]
        if sum(bool(re.search(r"\d[\d,]{2,}", l)) for l in body[:40]) >= 8:
            stop = next((j for j, l in enumerate(body) if j > 5 and re.match(r"^consolidated ", _norm(l))), len(body))
            return lines[start : start + 1 + stop]
        i = start + 1
    return []


def prepare(c: Company, count: Counter) -> tuple[str, dict, dict]:
    lines = (FILINGS / "raw" / c.raw).read_text(errors="replace").splitlines()
    parts: list[tuple[str, str, list[tuple[str, str]]]] = []
    for key, title in (("item1", "Item 1 — Business (opening)"), ("item1a", "Item 1A — Risk Factors (opening)"),
                       ("item7", "Item 7 — Management's Discussion and Analysis (opening)")):
        paras = take(paragraphs(section_lines(lines, c, key)), BUDGET[key], count)
        parts.append((key, title, anchored(c.ticker, key, paras, count)))
    for name, pattern in STATEMENTS:
        rows = statement_lines(lines, pattern)
        if not rows:
            print(f"  {c.ticker}: statement {name} not found")
            continue
        paras = take(paragraphs(rows, table=True), BUDGET["statement"], count)
        title = {"income": "Item 8 — Consolidated statement of operations / income", "balance": "Item 8 — Consolidated balance sheet",
                 "cashflow": "Item 8 — Consolidated statement of cash flows"}[name]
        parts.append((f"item8-{name}", title, anchored(c.ticker, f"item8-{name}", paras, count)))

    md = [f"# {c.name} — Annual Report on Form 10-K, {c.fiscal_year} (period ended {c.period_end})",
          "",
          "Excerpt for analysis, cut to the same rule for every company: the opening of Item 1 (Business), "
          "the opening of Item 1A (Risk Factors), the opening of Item 7 (MD&A) and the three primary financial "
          "statements from Item 8. Each passage starts with its anchor in [brackets]; cite anchors exactly.",
          ""]
    index: dict[str, dict] = {}
    sizes: dict[str, int] = {}
    for key, title, chunks in parts:
        md += [f"## {title}", ""]
        for anchor, text in chunks:
            md += [f"[{anchor}]", _wrap(text), ""]
            index[anchor] = {"company": c.ticker, "section": title, "text": text}
        sizes[key] = sum(count(t) for _, t in chunks)
    doc = "\n".join(md)
    meta = {"ticker": c.ticker, "name": c.name, "fiscal_year": c.fiscal_year, "period_end": c.period_end,
            "source": c.source, "tokens": count(doc), "section_tokens": sizes, "anchors": len(index)}
    return doc, index, meta


def main() -> None:
    count = Counter()
    index: dict[str, dict] = {}
    metas = []
    for c in COMPANIES:
        doc, idx, meta = prepare(c, count)
        (FILINGS / f"{c.ticker}.md").write_text(doc)
        index.update(idx)
        metas.append(meta)
        print(f"{c.ticker}: {meta['tokens']:,} tokens, {meta['anchors']} anchors {meta['section_tokens']}")
    (FILINGS / "index.json").write_text(json.dumps(index, indent=1))
    (FILINGS / "manifest.json").write_text(json.dumps(metas, indent=2))
    print(f"wrote {FILINGS}/<ticker>.md, index.json, manifest.json")


if __name__ == "__main__":
    main()
