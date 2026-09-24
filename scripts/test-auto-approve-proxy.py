#!/usr/bin/env python3
"""
Tests the clean-review detection in fly/auto-approve-proxy.py.

Run with:  python scripts/test-auto-approve-proxy.py

No dependencies and no network access. The fixtures are real comment bodies:
CLEAN and WITH_FINDINGS from klikpeta-tech/pr-agent#2 (a review with findings
and the same persistent comment after the findings were fixed), and
CLEAN_NO_FINDINGS_ROW from klikpeta-tech/bdf-dbt#1 (a review where the model
returned no findings at all, which renders as a structurally different row).

This logic decides whether the app submits an APPROVE that counts toward branch
protection, so a wrong "clean" verdict is the expensive direction: it approves a
PR that has real findings. Every ambiguous case below must come out False.
"""

import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY_SRC = os.path.join(REPO_ROOT, "fly", "auto-approve-proxy.py")

failures: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r} want {want!r}")
    if not ok:
        failures.append(label)


# Real body of the persistent review once every finding was fixed: the
# focus-areas cell is emitted but empty.
CLEAN = """## PR Reviewer Guide 🔍

#### (Review updated until commit https://github.com/klikpeta-tech/pr-agent/commit/78f1ae5)


Here are some key observations to aid the review process:

<table>
<tr><td>⏱️&nbsp;<strong>Estimated effort to review</strong>: 4 🔵🔵🔵🔵⚪</td></tr>
<tr><td>🧪&nbsp;<strong>PR contains tests</strong></td></tr>
<tr><td>🔒&nbsp;<strong>No security concerns identified</strong></td></tr>
<tr><td>⚡&nbsp;<strong>Recommended focus areas for review</strong><br><br>

</td></tr>
</table>
"""

# Real body of a review where the model returned no findings at all — a
# structurally different clean shape than CLEAN above (captured from
# klikpeta-tech/bdf-dbt#1, comment 5790645315): pr-agent renders a dedicated
# "No major issues detected" row instead of an empty focus-areas cell.
CLEAN_NO_FINDINGS_ROW = """## PR Reviewer Guide 🔍

Here are some key observations to aid the review process:

<table>
<tr><td>⏱️&nbsp;<strong>Estimated effort to review</strong>: 4 🔵🔵🔵🔵⚪</td></tr>
<tr><td>🧪&nbsp;<strong>PR contains tests</strong></td></tr>
<tr><td>🔒&nbsp;<strong>No security concerns identified</strong></td></tr>
<tr><td>⚡&nbsp;<strong>No major issues detected</strong></td></tr>
</table>
"""

# Real body of the same review while it still had two findings.
WITH_FINDINGS = """## PR Reviewer Guide 🔍

Here are some key observations to aid the review process:

<table>
<tr><td>⏱️&nbsp;<strong>Estimated effort to review</strong>: 4 🔵🔵🔵🔵⚪</td></tr>
<tr><td>🧪&nbsp;<strong>PR contains tests</strong></td></tr>
<tr><td>🔒&nbsp;<strong>No security concerns identified</strong></td></tr>
<tr><td>⚡&nbsp;<strong>Recommended focus areas for review</strong><br><br>

<details><summary><a href='https://github.com/klikpeta-tech/pr-agent/pull/2/files#diff-b80148R257-R268'><strong>Oversized admission</strong></a>

The quota check only verifies that current `used + reserved` is below the cap
before admitting a request, but it does not verify that the new reservation
itself still fits.
</summary>

```python
    if self._committed_locked(1) < TIER_SPENDABLE[1]:
        self._reserved[1] += estimate
```
</details>

<details><summary><a href='https://github.com/klikpeta-tech/pr-agent/pull/2/files#diff-b80148R300'><strong>Midnight miscount</strong></a>

Requests that start before the daily rollover and finish after midnight are
charged to the new day.
</summary>
</details>

</td></tr>
</table>
"""

spec = importlib.util.spec_from_file_location("aap", PROXY_SRC)
aap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aap)

print("=== real captured review bodies ===")
check("clean review (empty focus cell) is clean", aap.review_is_clean(CLEAN), True)
check("clean review (no-findings row) is clean", aap.review_is_clean(CLEAN_NO_FINDINGS_ROW), True)
check("review with 2 findings is not clean", aap.review_is_clean(WITH_FINDINGS), False)

print("\n=== a single finding must still block ===")
one_finding = CLEAN.replace(
    "for review</strong><br><br>\n\n</td>",
    "for review</strong><br><br>\n\n<details><summary><strong>Something</strong>"
    "\n\nA real problem.</summary></details>\n\n</td>",
)
check("one finding is not clean", aap.review_is_clean(one_finding), False)

print("\n=== ambiguous shapes must fail safe (False) ===")
check("focus-areas section missing entirely", aap.review_is_clean("## PR Reviewer Guide 🔍"), False)
check("empty body", aap.review_is_clean(""), False)
check(
    "heading present but cell never closed",
    aap.review_is_clean("Recommended focus areas for review</strong><br><br>"),
    False,
)
check(
    "bare prose in the cell (markup changed shape)",
    aap.review_is_clean(
        "<td>Recommended focus areas for review</strong><br><br>\n"
        "Potential race condition in the counter\n</td>"
    ),
    False,
)
check(
    "the old prose phrase alone no longer approves",
    aap.review_is_clean("## PR Reviewer Guide 🔍\n\nNo major issues detected"),
    False,
)

print("\n=== whitespace-only variations are still clean ===")
for label, cell in [
    ("newlines only", "\n\n\n"),
    ("nbsp only", "&nbsp;"),
    ("br tags only", "<br><br>"),
    ("nothing at all", ""),
]:
    body = f"<td>⚡&nbsp;<strong>Recommended focus areas for review</strong>{cell}</td>"
    check(f"cell with {label}", aap.review_is_clean(body), True)

print("\n=== the old trigger constant is gone ===")
check("APPROVAL_TRIGGER removed", hasattr(aap, "APPROVAL_TRIGGER"), False)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL PASS")
