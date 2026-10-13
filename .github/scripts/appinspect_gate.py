"""Summarize AppInspect JSON reports as Markdown and gate on errors and failures.

Usage: python appinspect_gate.py [--title TITLE] <report.json>...

Prints a Markdown summary to stdout. Exits 1 if any report has an error or a
failure, or is missing. Warnings are listed but do not gate.
"""

import argparse
import json
import sys

GATED = ("failure", "error")
LISTED = ("failure", "error", "future_failure", "warning")


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", default="AppInspect")
    parser.add_argument("reports", nargs="+")
    args = parser.parse_args(argv)

    failed = False
    lines = [f"## {args.title}", ""]
    lines.append("| Report | Failures | Errors | Future failures | Warnings | Result |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    findings = {}  # (result, check name) -> report names, in first-seen order
    for path in args.reports:
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
                        key = (check["result"], check["name"])
                        findings.setdefault(key, []).append(name)
    if findings:
        lines += ["", "Findings (only failures and errors gate):", ""]
        for (result, check), names in sorted(findings.items(), key=lambda i: LISTED.index(i[0][0])):
            lines.append(f"- {result}: `{check}` ({', '.join(f'`{n}`' for n in names)})")
    print("\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
