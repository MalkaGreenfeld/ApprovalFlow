# The 4 Worked Journeys

Reference table for the D5 verification journeys run by `python eval/harness.py`.

| # | Journey | Invoice ID | Vendor | Category | Amount | Route | What it proves |
|---|---------|-----------|--------|----------|--------|-------|----------------|
| A | Auto-Approve | INV-1001 | Bistro 19 | meals | $42.00 | auto_approve | In-policy meal, under ceiling, known vendor — fully autonomous path |
| B | Escalate & Resume | INV-1003 | The Rooftop Grill | meals | $1,820.00 | human_review | Client entertainment over $500 without required justification — needs human approval, then resumes |
| C | Duplicate Detection | INV-1007 | Bistro 19 | meals | $42.00 | duplicate | Exact re-submission of INV-1001 (same vendor + invoice # + total) — blocked before a second payment |
| D | Payment Failure & Compensation | INV-1012 | RackSpace Supplies | hardware | $9,500.00 | human_review → payment fails | Human-approved capital hardware; payment is forced to fail so the saga compensates (releases reservation, marks payment-failed) |

Source: [sample-invoices.json](../sample-invoices.json), matching the 4 journeys run via `python eval/harness.py --journey {A,B,C,D}` per [CLAUDE.md](../CLAUDE.md).
