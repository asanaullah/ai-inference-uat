#!/usr/bin/env python3
# Assisted by Claude Opus 4.6
"""Aggregate JUnit XML results into a summary JSON report."""

import json
import os
import sys
import xml.etree.ElementTree as ET


def parse_junit(path):
    tree = ET.parse(path)
    root = tree.getroot()

    if root.tag == 'testsuites':
        suites = root.findall('testsuite')
    elif root.tag == 'testsuite':
        suites = [root]
    else:
        return {'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0}

    tests = failures = errors = skipped = 0
    for suite in suites:
        tests += int(suite.get('tests', 0))
        failures += int(suite.get('failures', 0))
        errors += int(suite.get('errors', 0))
        skipped += int(suite.get('skipped', 0))

    return {
        'tests': tests,
        'failures': failures,
        'errors': errors,
        'skipped': skipped,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: aggregate.py <results-dir>", file=sys.stderr)
        sys.exit(1)

    results_dir = sys.argv[1]
    report_dir = os.path.join(results_dir, 'report')
    os.makedirs(report_dir, exist_ok=True)

    print(f"Aggregating results from {results_dir}")

    totals = {'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0}
    entries = []

    scope_dirs = ['node', 'cluster', 'project']
    for scope in scope_dirs:
        scope_path = os.path.join(results_dir, scope)
        if not os.path.isdir(scope_path):
            continue
        for dirpath, _dirnames, filenames in sorted(os.walk(scope_path)):
            if 'junit.xml' not in filenames:
                continue

            junit_path = os.path.join(dirpath, 'junit.xml')
            rel_path = os.path.relpath(dirpath, scope_path)
            entry_name = f'{scope}/{rel_path}'

            counts = parse_junit(junit_path)
            for key in totals:
                totals[key] += counts[key]

            status = 'failed' if counts['failures'] or counts['errors'] else 'passed'
            entries.append({'name': entry_name, **counts, 'status': status})

    overall = 'failed' if totals['failures'] or totals['errors'] else 'passed'
    summary = {'status': overall, 'totals': totals, 'entries': entries}

    summary_path = os.path.join(report_dir, 'summary.json')
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2)

    print(f"Summary written to {summary_path}")
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
