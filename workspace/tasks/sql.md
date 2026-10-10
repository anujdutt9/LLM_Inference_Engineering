Investigate what happened to NovaAI's business and platform from March to August 2026, using the operations database data/nova_ai.db.

Run a team of 4 sql-analyst agents in parallel, one per area:
1. revenue, plans and churn
2. usage, latency and model quality
3. incidents and deployments
4. support tickets and customer feedback

When they report back, reconcile their findings: where two areas point to the same cause, connect them; where they disagree, check the numbers with one more sql-analyst. Then write the report, in both formats:
- {{OUT}}/nova_report.md
- {{OUT}}/nova_report.html (self-contained, no external assets)

The report: a one-paragraph summary; a timeline of what changed and when; the 5 most material findings, each with its numbers, the SQL that shows it and what caused it; and open questions.
