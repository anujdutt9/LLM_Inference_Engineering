NovaAI's billing library in code/ledger is unfinished and buggy: some tests fail, some functions aren't written yet (they raise NotImplementedError), and the tests don't catch every bug. Each module's docstring is its specification.

First copy it to your own working copy with exactly this command, nothing before it: `cp -R code/ledger {{OUT}}/ledger` (it creates {{OUT}}/ledger; leave code/ledger unchanged). Then work only there, and give each code-fixer the package directory {{OUT}}/ledger.

Run a team of 4 code-fixer agents in parallel, one per module of {{OUT}}/ledger/ledger/:
1. pricing.py (tests/test_pricing.py)
2. tax.py (tests/test_tax.py)
3. discounts.py (tests/test_discounts.py)
4. invoices.py (tests/test_invoices.py)

Each one, in its module: fixes the bugs the tests show; reviews every function against the module's docstring and fixes the bugs no test catches; implements the unfinished functions to the docstring; and adds a test for each bug it fixed and each function it wrote, in a new file tests/test_<module>_extra.py (the existing test files stay unchanged).

When they report back, run the whole test suite from {{OUT}}/ledger (`python3 -m pytest -q -p no:cacheprovider`) and review each module against its docstring yourself: a fix must follow the documented rule, not just make a test pass. Fix anything that is still wrong. Then write {{OUT}}/fix_report.md: the final test result; each bug (module, function, what was wrong, which documented rule it broke, the fix, and whether a test caught it); each function implemented; and anything you are unsure about.
