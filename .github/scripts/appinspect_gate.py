"""Summarize AppInspect JSON reports as Markdown and gate on errors and failures.

Usage: python appinspect_gate.py <report.json>...

Prints a Markdown summary to stdout. Exits 1 if any report has an error or a
failure, or is missing. Warnings are listed but do not gate.
"""

import json
import sys

GATED = ("failure", "error")
LISTED = ("failure", "error", "future_failure", "warning")


def main(paths):
    failed = False
    lines = ["## AppInspect", ""]
    lines.append("| Report | Failures | Errors | Future failures | Warnings | Result |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    findings = []
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        try:
            with open(path) as f:
                report = json.load(f)
        except (OSError, ValueError) as e:
            failed = True
            lines.append(f"| `{name}` | - | - | - | - | missing or unreadable: {e} |")
            continue
        summary = report.get("summary", {})
        gate = sum(summary.get(k, 0) for k in GATED)
        failed = failed or gate > 0
        lines.append(
            f"| `{name}` | {summary.get('failure', 0)} | {summary.get('error', 0)} "
            f"| {summary.get('future_failure', 0)} | {summary.get('warning', 0)} "
            f"| {'FAIL' if gate else 'pass'} |"
        )
        for app in report.get("reports", []):
            for group in app.get("groups", []):
                for check in group.get("checks", []):
                    if check.get("result") in LISTED:
                        findings.append(f"- `{name}` {check['result']}: `{check['name']}`")
    if findings:
        lines += ["", "Findings (only failures and errors gate):", "", *findings]
    print("\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
