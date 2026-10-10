"""analysis/grade.py: the report scores are right on known inputs."""

from __future__ import annotations

import json
from pathlib import Path

from analysis.grade import grade_10k, grade_results, grade_sql, numbers

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "datasets" / "exp1a" / "benchmark_questions.jsonl"
INDEX = ROOT / "datasets" / "exp1b" / "filings" / "index.json"


def test_numbers_keep_their_written_precision():
    got = [(n.value, n.unit) for n in numbers("$702K, 7.76 s, 24.6%, 1,435,032 tokens, 130.5 billion")]
    assert got == [(702000.0, 1000.0), (7.76, 0.01), (24.6, 0.1), (1435032.0, 1.0), (130.5e9, 1e8)]


def test_ground_truth_recovers_every_story_and_noise_none():
    if not QUESTIONS.exists():
        return
    q = [json.loads(line) for line in QUESTIONS.read_text().splitlines()]
    gt = "\n\n".join(x["ground_truth_answer"] + "\n" + "\n".join(x["expected_facts"]) for x in q if x["related_stories"])
    g = grade_sql(gt)
    assert g["fact_recall"] == 1.0 and g["stories_recovered"] == g["stories_total"]
    assert grade_sql("The company did fine. Revenue grew 3% in 2026.")["facts_found"] == 0


def test_sql_queries_are_run_read_only(tmp_path):
    g = grade_sql("```sql\nSELECT COUNT(*) FROM usage_events;\nSELECT nope FROM missing;\n```")
    if (ROOT / "datasets" / "exp1a" / "nova_ai.db").exists():
        assert (g["sql_run_ok"], g["sql_queries"]) == (1, 2)


def test_10k_citations_quotes_and_numbers_are_checked():
    index = {
        "nvda-item1-p001": {"text": "NVIDIA is now a data center scale AI infrastructure company reshaping all industries."},
        "nvda-item8-income-p001": {"text": "Revenue | $ | 215,938 | $ | 130,497"},
    }
    rep = (
        "| NVIDIA | $215.9 billion, up from $130.5 billion | [nvda-item8-income-p001] |\n"
        "| NVIDIA | invented 999,999 in revenue last year somehow | [nvda-item8-income-p001] |\n\n"
        '- "NVIDIA is now a data center scale AI infrastructure company" [nvda-item1-p001]\n'
        '- "we are the best at everything forever and always" [nvda-item1-p001]\n'
        "- Broadcom said something [avgo-item9-p001]\n"
    )
    findings = [{"source_anchor": "nvda-item1-p001", "evidence": "reshaping all industries"},
                {"source_anchor": "nvda-item1-p001", "evidence": "a made-up sentence here"}]
    g = grade_10k(rep, findings, index)
    assert (g["citations_valid"], g["citations"]) == (4, 5)
    assert (g["quotes_verbatim"], g["quotes_checked"]) == (1, 2)
    assert (g["numbers_in_cited_passage"], g["numbers_checked"]) == (2, 3)
    assert (g["findings_evidence_verbatim"], g["analyst_findings"]) == (1, 2)
    assert g["companies_covered"] == ["nvda"]


def test_results_pairs_reports_with_their_sessions(tmp_path):
    if not INDEX.exists():
        return
    (tmp_path / "reports" / "exp1b").mkdir(parents=True)
    (tmp_path / "reports" / "exp1b" / "10k_report.md").write_text("NVIDIA [nvda-item1-p001]\n")
    (tmp_path / "harness").mkdir()
    blk = '```json\n[{"source_anchor": "nvda-item1-p001", "evidence": "NVIDIA pioneered accelerated computing"}]\n```'
    lines = [{"event": "user", "text": "write reports/exp1b/10k_report.md"}, {"event": "tool_result", "text": blk}]
    (tmp_path / "harness" / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    rows, md = grade_results(tmp_path)
    assert rows[0]["label"] == "exp1b" and rows[0]["findings_evidence_verbatim"] == 1
    assert "10-K reports" in md
