Investigate what happened to NovaAI's business and platform from March to August 2026, using the operations database data/nova_ai.db.

Run a team of 8 sql-analyst agents in parallel, one per area:
1. revenue and billing
2. plans, upgrades and churn
3. usage and traffic
4. latency and model quality
5. incidents
6. deployments and configuration changes
7. support tickets
8. customer feedback and satisfaction

When they report back, reconcile their findings: where two areas point to the same cause, connect them; where they disagree, check the numbers with one more sql-analyst. Then write the report, in both formats:
- {{OUT}}/nova_report.md
- {{OUT}}/nova_report.html (self-contained, no external assets)

The report: a one-paragraph summary; a timeline of what changed and when; the 5 most material findings, each with its numbers, the SQL that shows it and what caused it; and open questions.
