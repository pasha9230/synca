"""The test files of one part of the GitHub run (.github/workflows/tests.yml runs the parts side by side).

    python scripts/ci_shard.py <parts> <index>      # the files of part <index> (0-based), one per line
    python scripts/ci_shard.py <parts> --all        # every part: "<index> <file>" lines (the final check uses it)

The split is by time: the files, longest first, each to the part that has the least so far (scripts/ci_durations.json,
seconds per file from a full run; a file not in it counts as the median one). Every tests/test_*.py goes to exactly one
part and the split is the same on every machine. Refresh the times from the junit reports of a GitHub run (the artifacts
junit-0…3 of Actions → tests): python scripts/ci_shard.py --durations junit-*.xml
"""
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DURATIONS = Path(__file__).with_name("ci_durations.json")


def files() -> list:
    return sorted(str(p) for p in Path("tests").glob("test_*.py"))


def split(parts: int) -> list:
    known = json.loads(DURATIONS.read_text(encoding="utf-8")) if DURATIONS.exists() else {}
    usual = statistics.median(known.values()) if known else 1.0
    load, out = [0.0] * parts, [[] for _ in range(parts)]
    for name in sorted(files(), key=lambda f: (-known.get(f, usual), f)):
        part = min(range(parts), key=lambda i: (load[i], i))
        load[part] += known.get(name, usual)
        out[part].append(name)
    return [sorted(p) for p in out]


def durations(reports: list) -> None:
    per_file = {}
    for case in (c for junit in reports for c in ET.parse(junit).iter("testcase")):
        name = "tests/%s.py" % case.get("classname", "").split(".")[1] if case.get("classname", "").startswith("tests.") else None
        if name:
            per_file[name] = round(per_file.get(name, 0.0) + float(case.get("time") or 0), 2)
    DURATIONS.write_text(json.dumps(dict(sorted(per_file.items())), indent=0, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1] == "--durations":
        durations(sys.argv[2:])
        sys.exit(0)
    parts = int(sys.argv[1])
    shares = split(parts)
    if sys.argv[2] == "--all":
        print("\n".join("%d %s" % (i, f) for i, share in enumerate(shares) for f in share))
    else:
        print("\n".join(shares[int(sys.argv[2])]))
