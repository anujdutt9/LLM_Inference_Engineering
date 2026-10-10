"""Quality scores for the agent reports, so a faster run can't win by doing less.

SQL report (Tenant A): which of the dataset's planted stories it recovered. Each story has
a few key facts (from the benchmark's `expected_facts`, every one reproducible from its
`verification_sql`); a fact counts when its numbers and names appear together in one
paragraph, table or list of the report. Plus: how many of the report's SQL queries run.

10-K report (Tenant B): every cited anchor exists; companies covered; quoted evidence found
verbatim in the passage it cites; numbers in cited lines found in the cited passage (the
rest are computed, e.g. growth rates, or unsupported); share of claim lines with a citation.
The analysts' own findings (JSON with `evidence` + `source_anchor`, from the tinyharness
transcripts) are checked the same way.

    python -m analysis.grade results                        # every report under results/reports/
    python -m analysis.grade sql path/to/nova_report.md
    python -m analysis.grade 10k path/to/10k_report.md [transcript.jsonl ...]

Writes results/analysis/quality.md and quality.json (the `results` form).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "datasets" / "exp1a" / "nova_ai.db"
INDEX = ROOT / "datasets" / "exp1b" / "filings" / "index.json"
TICKERS = ("nvda", "amd", "intc", "avgo")
ANCHOR = re.compile(r"\b((?:nvda|amd|intc|avgo)-item[0-9a-z]+(?:-[a-z]+)?-p\d{3})\b")

# ---- numbers --------------------------------------------------------------------------------

_NUM = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?:\s*([kKmMbB])(?![A-Za-z])|\s*(thousand|million|billion)\b)?"
)
_SCALE = {"k": 1e3, "m": 1e6, "b": 1e9, "thousand": 1e3, "million": 1e6, "billion": 1e9}


@dataclass(frozen=True)
class Num:
    value: float
    unit: float  # the precision it was written with (0.1 for "24.6", 1000 for "702K")


def numbers(text: str) -> list[Num]:
    out = []
    for m in _NUM.finditer(text):
        whole, frac, suffix, word = m.groups()
        scale = _SCALE[(suffix or word or "").lower()] if (suffix or word) else 1.0
        v = float(whole.replace(",", "") + ("." + frac if frac else ""))
        unit = 10.0 ** -(len(frac) if frac else 0)
        out.append(Num(v * scale, unit * scale))
    return out


def close(written: Num, truth: float) -> bool:
    """The report's number is the fact: within 0.5 %, or the fact rounded to the precision it
    was written with ("25%" for 24.6 %, "$702K"), as long as that rounding is under 2 %."""
    diff = abs(written.value - truth)
    return diff <= 0.005 * abs(truth) or (diff <= written.unit / 2 * 1.0001 and diff <= 0.02 * abs(truth))


# ---- SQL report: planted stories ------------------------------------------------------------
# A fact is a list of alternatives; an alternative is a list of atoms that must all appear in
# the same block (paragraph / table / list) of the report. An atom is a number (float) or a
# case-insensitive regex (str).

def _date(y: int, m: int, d: int) -> str:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    mon = months[m - 1]
    return rf"(?:{y}-{m:02d}-{d:02d}|{mon}[a-z]*\.?\s+{d}\b|\b{d}\s+{mon}|\b{m}/{d}\b)"


USW = r"US.?West"
STORIES: dict[str, dict] = {
    "A": {"title": "US-West routing imbalance, Jul 14–16 (SEV1, credits)", "facts": {
        "SEV1 incident INC-2026-0109": [[r"INC-2026-0109"], [r"SEV.?1", USW]],
        "started Jul 14 in US-West": [[_date(2026, 7, 14), USW]],
        "Enterprise credits $153,896 (80 customers)": [[153896.0], [80.0, r"credit"]],
        "US-West revenue −23.9%": [[23.9, USW], [2114636.0, 1610374.0]],
    }},
    "B": {"title": "Long-context (>16K) latency regression from Jun 3 → churn", "facts": {
        "inference-engine v2.8.0 on Jun 3": [[r"v?2\.8\.0"], [_date(2026, 6, 3), r"16\s*K|16,000|long"]],
        ">16K P95 2,893 → 7,762 ms": [[7762.4], [7.76, r"p95"]],
        "6 churned Enterprise customers, $702K MCV": [[702000.0]],
        "named churners (Quantix, Meridian, …)": [[r"Quantix", r"Meridian"]],
    }},
    "A+B": {"title": "Enterprise revenue −11.5% in July", "facts": {
        "Enterprise revenue −11.5%": [[11.5, r"revenue"], [5100996.0, 4514832.0]],
        "uncredited SLA breaches ($15,300 exposure)": [[15300.0], [r"uncredited|not credited|never credited", r"breach"]],
    }},
    "C": {"title": "Prefix cache v2 on Aug 10: hit rate 25% → 51%", "facts": {
        "cache-hit 24.6% → 51.1%": [[51.1, r"cache"]],
        "on Aug 10": [[_date(2026, 8, 10), r"cache"]],
        "lower TTFT / GPU utilization": [[231.6, 205.2], [41.4, 36.8]],
    }},
    "D": {"title": "nova-reasoning vs nova-small trade-off", "facts": {
        "ratings 3.62 vs 3.84": [[3.62, 3.84]],
        "cost $0.03468 vs $0.00036 per request": [[0.03468], [0.035, r"nova-reasoning"]],
        "output 1,409 vs 197 tokens": [[1409.0, 197.0]],
    }},
    "E": {"title": "Noisy neighbor: Helix Genomics batch jobs, US-East, Aug", "facts": {
        "Helix Genomics named": [[r"Helix"]],
        "P99 ×14.4 vs P50 ×1.7": [[14.43], [14.4, r"p99"]],
        "P99 1,335 → 19,259 ms": [[19259.2], [19.3, r"p99"]],
        "Helix August invoice $540K / requests 129 → 7,946": [[540161.0], [7946.0]],
    }},
    "F": {"title": "Europe rate-limiter misconfiguration, May 19–21", "facts": {
        "429 share 29.7%": [[29.7, r"rate.?limit|429"]],
        "rate limiter v3.4.0 (rolled back)": [[r"v?3\.4\.0"]],
        "35 customers got 429s": [[35.0, r"429|customer"]],
        "May 19 in Europe": [[_date(2026, 5, 19), r"Europe"]],
    }},
    "sat": {"title": "Satisfaction dip Jun–Jul from latency, not quality or price", "facts": {
        "June rating 3.37, latency share 39.9%": [[3.37], [39.9, r"latency"]],
    }},
}


def blocks(text: str) -> list[str]:
    """Paragraphs, tables and lists: split on blank lines (and HTML block tags)."""
    text = re.sub(r"<(?:/p|br|/tr|/li|/h\d|/div)\s*/?>", "\n\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return [b for b in re.split(r"\n\s*\n", text) if b.strip()]


def _atom_in(atom: float | str, block: str, nums: list[Num]) -> bool:
    if isinstance(atom, str):
        return re.search(atom, block, re.I) is not None
    return any(close(n, atom) for n in nums)


def fact_found(alternatives: list[list], bl: list[tuple[str, list[Num]]]) -> bool:
    return any(all(_atom_in(a, b, n) for a in alt) for alt in alternatives for b, n in bl)


SQL_BLOCK = re.compile(r"```sql\s*(.*?)```", re.S | re.I)
SQL_INLINE = re.compile(r"((?:SELECT|WITH)\b[^;`]*?;)", re.S | re.I)


def sql_queries(text: str) -> list[str]:
    found = [q.strip() for blk in SQL_BLOCK.findall(text) for q in blk.split(";") if q.strip()]
    if not found:
        found = [q.strip().rstrip(";") for q in SQL_INLINE.findall(text)]
    return found


def run_sql(queries: list[str], db: Path = DB) -> tuple[int, list[str]]:
    """How many of the report's queries run read-only against the dataset."""
    if not db.exists():
        return 0, ["database not found"]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    ok, errors = 0, []
    for q in queries:
        try:
            con.execute(q).fetchmany(1)
            ok += 1
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
    con.close()
    return ok, errors


def grade_sql(text: str, db: Path = DB) -> dict:
    bl = [(b, numbers(b)) for b in blocks(text)]
    stories = {}
    found_total = facts_total = 0
    for key, story in STORIES.items():
        facts = {name: fact_found(alts, bl) for name, alts in story["facts"].items()}
        n = sum(facts.values())
        found_total += n
        facts_total += len(facts)
        stories[key] = {"title": story["title"], "found": n, "of": len(facts), "facts": facts,
                        "recovered": n * 2 >= len(facts)}
    queries = sql_queries(text)
    ok, errors = run_sql(queries, db)
    return {
        "kind": "sql",
        "fact_recall": round(found_total / facts_total, 3),
        "facts_found": found_total,
        "facts_total": facts_total,
        "stories_recovered": sum(s["recovered"] for s in stories.values()),
        "stories_total": len(stories),
        "stories": stories,
        "sql_queries": len(queries),
        "sql_run_ok": ok,
        "sql_errors": errors[:5],
    }


# ---- 10-K report: citations -----------------------------------------------------------------

def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[–—]", "-", s)
    return re.sub(r"\s+", " ", s).strip(" .,;:\"'")


def quote_in(quote: str, passages: list[str]) -> bool:
    parts = [p for p in (_norm(x) for x in re.split(r"\.\.\.|…|\[\.\.\.\]", quote)) if len(p) >= 8]
    return bool(parts) and any(all(p in _norm(t) for p in parts) for t in passages)


def number_in(n: Num, passages: list[str]) -> bool:
    """Filing tables are in millions (sometimes thousands); the report may write $130.5B."""
    for t in passages:
        for p in numbers(t):
            for scale in (1.0, 1e3, 1e6):
                if close(n, p.value * scale):
                    return True
    return False


QUOTE = re.compile(r"[\"“]([^\"“”]{20,400})[\"”]")
YEAR = re.compile(r"^(?:19|20)\d{2}$")


def _claim_lines(text: str) -> list[str]:
    text = re.sub(r"<[^>]+>", " ", text)
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or set(s) <= set("|-: "):
            continue
        if len(s.split()) >= 8 or re.search(r"\d", s):
            out.append(s)
    return out


def grade_10k(text: str, findings: list[dict] | None = None, index: dict | None = None) -> dict:
    index = index if index is not None else json.loads(INDEX.read_text())
    cited = ANCHOR.findall(text)
    valid = [a for a in cited if a in index]
    covered = sorted({a.split("-")[0] for a in valid})
    lines = _claim_lines(text)
    with_cite = [l for l in lines if ANCHOR.search(l)]
    quotes_ok = quotes = nums_ok = nums = 0
    for line in with_cite:
        passages = [index[a]["text"] for a in ANCHOR.findall(line) if a in index]
        if not passages:
            continue
        for q in QUOTE.findall(line):
            quotes += 1
            quotes_ok += quote_in(q, passages)
        clean = ANCHOR.sub(" ", line)
        for n in numbers(clean):
            if n.unit >= 1 and n.value < 10 or YEAR.match(f"{n.value:.0f}") and n.unit == 1:
                continue  # list numbers, small counts, fiscal years
            nums += 1
            nums_ok += number_in(n, passages)
    out = {
        "kind": "10k",
        "citations": len(cited),
        "citations_valid": len(valid),
        "citation_validity": round(len(valid) / len(cited), 3) if cited else None,
        "companies_covered": covered,
        "company_coverage": round(len(covered) / len(TICKERS), 3),
        "claim_lines": len(lines),
        "claim_lines_cited": len(with_cite),
        "cited_share": round(len(with_cite) / len(lines), 3) if lines else None,
        "quotes_checked": quotes,
        "quotes_verbatim": quotes_ok,
        "numbers_checked": nums,
        "numbers_in_cited_passage": nums_ok,
        "numbers_grounded_share": round(nums_ok / nums, 3) if nums else None,
    }
    if findings is not None:
        f_ok = f_anchor = 0
        for f in findings:
            a = str(f.get("source_anchor", "")).strip("[] ")
            if a in index:
                f_anchor += 1
                f_ok += quote_in(str(f.get("evidence", "")), [index[a]["text"]])
        out.update({"analyst_findings": len(findings), "findings_anchor_valid": f_anchor,
                    "findings_evidence_verbatim": f_ok,
                    "findings_evidence_share": round(f_ok / len(findings), 3) if findings else None})
    return out


# ---- transcripts ----------------------------------------------------------------------------

JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.S)


def _strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return []


def transcript_text(path: Path) -> list[str]:
    out = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            out.extend(_strings(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return out


def findings_from(strings: list[str]) -> list[dict]:
    """Every distinct {evidence, source_anchor} finding in ```json blocks."""
    seen, out = set(), []
    for s in strings:
        for blk in JSON_BLOCK.findall(s):
            try:
                data = json.loads(blk)
            except json.JSONDecodeError:
                continue
            for f in data if isinstance(data, list) else [data]:
                if isinstance(f, dict) and "source_anchor" in f and "evidence" in f:
                    key = (str(f["source_anchor"]), str(f["evidence"]))
                    if key not in seen:
                        seen.add(key)
                        out.append(f)
    return out


def sessions_by_label(harness: Path) -> dict[str, list[Path]]:
    """Transcript files per report label: the canonical prompts name reports/<label>/."""
    out: dict[str, list[Path]] = {}
    for p in sorted(harness.rglob("*.jsonl")):
        head = p.read_text(errors="replace")[:20000]
        for label in set(re.findall(r"reports/([\w.-]+)/(?:nova|10k)_report", head)):
            out.setdefault(label, []).append(p)
    return out


# ---- driver ---------------------------------------------------------------------------------

def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def grade_results(results: Path) -> tuple[list[dict], str]:
    reports = results / "reports"
    by_label = sessions_by_label(results / "harness") if (results / "harness").exists() else {}
    rows: list[dict] = []
    for md in sorted(reports.rglob("*_report.md")):
        label = md.parent.name
        text = md.read_text(errors="replace")
        if md.name.startswith("nova"):
            g = grade_sql(text)
        else:
            strings = [s for p in by_label.get(label, []) for s in transcript_text(p)]
            g = grade_10k(text, findings_from(strings) if strings else None)
        rows.append({"label": label, "report": str(md.relative_to(results)), **g})
    lines = ["# Report quality", "",
             "Scored by `analysis/grade.py` against the dataset (SQL) and the filings (10-K). See its docstring for the rules.", ""]
    sql = [r for r in rows if r["kind"] == "sql"]
    if sql:
        keys = list(STORIES)
        lines += ["## SQL reports: planted stories recovered", "",
                  "| run | fact recall | stories | " + " | ".join(keys) + " | SQL runs |",
                  "|---|---|---|" + "---|" * len(keys) + "---|"]
        for r in sql:
            cells = [f"{r['stories'][k]['found']}/{r['stories'][k]['of']}" for k in keys]
            lines.append(f"| {r['label']} | {_pct(r['fact_recall'])} ({r['facts_found']}/{r['facts_total']}) | "
                         f"{r['stories_recovered']}/{r['stories_total']} | " + " | ".join(cells) +
                         f" | {r['sql_run_ok']}/{r['sql_queries']} |")
        lines += ["", "Stories: " + "; ".join(f"**{k}** {v['title']}" for k, v in STORIES.items()), ""]
        for r in sql:
            missed = [f"{k}: {f}" for k, s in r["stories"].items() for f, ok in s["facts"].items() if not ok]
            if missed:
                lines.append(f"- {r['label']} missed: " + "; ".join(missed))
        lines.append("")
    tenk = [r for r in rows if r["kind"] == "10k"]
    if tenk:
        lines += ["## 10-K reports: citations", "",
                  "| run | citations valid | companies | claim lines cited | quotes verbatim | numbers in cited passage | analyst evidence verbatim |",
                  "|---|---|---|---|---|---|---|"]
        for r in tenk:
            fe = (f"{r['findings_evidence_verbatim']}/{r['analyst_findings']}" if "analyst_findings" in r else "—")
            lines.append(f"| {r['label']} | {r['citations_valid']}/{r['citations']} | {len(r['companies_covered'])}/4 "
                         f"({', '.join(r['companies_covered'])}) | {_pct(r['cited_share'])} | "
                         f"{r['quotes_verbatim']}/{r['quotes_checked']} | {r['numbers_in_cited_passage']}/{r['numbers_checked']} | {fe} |")
        lines += ["", "Numbers not in the cited passage are either computed (growth, margins) or unsupported: read those lines.", ""]
    if not rows:
        lines.append("No reports found under results/reports/ (run experiments/collect.sh).")
    return rows, "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] in ("sql", "10k"):
        text = Path(argv[1]).read_text(errors="replace")
        if argv[0] == "sql":
            out = grade_sql(text)
        else:
            strings = [s for p in argv[2:] for s in transcript_text(Path(p))]
            out = grade_10k(text, findings_from(strings) if argv[2:] else None)
        print(json.dumps(out, indent=2))
        return 0
    results = Path(argv[0] if argv else "results")
    rows, md = grade_results(results)
    dest = results / "analysis"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "quality.json").write_text(json.dumps(rows, indent=2))
    (dest / "quality.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
