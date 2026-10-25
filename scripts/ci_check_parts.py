"""The final check of the GitHub run: the parts together ran every test once and nothing else.

    python scripts/ci_check_parts.py <parts> <folder with junit-<index>.xml>

Fails (exit 1) when a part's report is missing, when the split leaves a test file out or puts one in two parts, when a test
was reported twice, or when the sum of the tests of the parts differs from what `pytest --collect-only` collects.
"""
import collections
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ci_shard  # noqa: E402

parts, folder = int(sys.argv[1]), Path(sys.argv[2])
errors = []

shares = ci_shard.split(parts)
placed = collections.Counter(f for share in shares for f in share)
missing_files = sorted(set(ci_shard.files()) - set(placed))
twice_files = sorted(f for f, n in placed.items() if n > 1)
if missing_files or twice_files:
    errors.append("split: files out %s, in two parts %s" % (missing_files, twice_files))

seen, per_part = collections.Counter(), {}
for index in range(parts):
    report = folder / ("junit-%d.xml" % index)
    if not report.exists():
        errors.append("part %d: no report %s" % (index, report))
        continue
    cases = [(c.get("classname"), c.get("name")) for c in ET.parse(report).iter("testcase")]
    per_part[index] = len(cases)
    seen.update(cases)

collecting = subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"], capture_output=True, text=True)
found = re.search(r"^(\d+) tests? collected", collecting.stdout, re.M)
collected = int(found.group(1)) if found and collecting.returncode == 0 else -1
ran = sum(seen.values())
twice = [case for case, n in seen.items() if n > 1]

print("parts: %s" % ", ".join("%d: %d" % (i, n) for i, n in sorted(per_part.items())))
print("sum of the parts: %d; collected: %d" % (ran, collected))
if collected < 0:
    errors.append("pytest --collect-only failed (code %d) or gave no count" % collecting.returncode)
if twice:
    errors.append("reported twice: %s" % twice[:10])
if ran != collected:
    errors.append("the parts ran %d tests, the suite has %d" % (ran, collected))
if errors:
    print("FAILED:\n- " + "\n- ".join(errors))
    sys.exit(1)
print("OK: every test ran once")
