"""Temporary (issue 168): run the files the windows job leaves out, one pytest per file,
with a per-test timeout. A test that times out is named, deselected and the file rerun."""

import re
import subprocess
import sys

FILES = sys.argv[1:] or [
    "tests/test_reconnect.py",
    "tests/test_resume.py",
    "tests/test_calendar.py",
    "tests/test_instruments.py",
    "tests/test_new_message_types.py",
    "tests/test_tickets.py",
    "tests/test_examples.py",
]
RESULT = re.compile(r"^(tests/\S+::\S+)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)")
START = re.compile(r"^(tests/\S+::\S+)\s*$|^(tests/\S+::\S+) $")

summary = []
for path in FILES:
    hung: list[str] = []
    for attempt in range(6):
        cmd = [
            sys.executable, "-m", "pytest", path, "-v", "-p", "no:cacheprovider",
            "--timeout=90", "--timeout-method=thread", "-o", "faulthandler_timeout=60",
            "--tb=short", "-rfE",
        ]
        for nodeid in hung:
            cmd += ["--deselect", nodeid]
        print("::group::" + " ".join(cmd), flush=True)
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = proc.stdout + proc.stderr
        print(out, flush=True)
        print("::endgroup::", flush=True)
        if "+ Timeout +" not in out:
            failed = [m.group(1) for line in out.splitlines() if (m := RESULT.match(line)) and m.group(2) in ("FAILED", "ERROR")]
            summary.append(f"{path}: rc={proc.returncode} hung={hung} failed={failed}")
            break
        started = None
        for line in out.splitlines():
            stripped = line.rstrip()
            if stripped.startswith("tests/") and "::" in stripped and not RESULT.match(line):
                started = stripped.split()[0]
        if started is None or started in hung:
            summary.append(f"{path}: rc={proc.returncode} could not name the hung test; hung={hung}")
            break
        hung.append(started)
    else:
        summary.append(f"{path}: gave up; hung={hung}")

print("\n==== SUMMARY ====")
for line in summary:
    print(line)
