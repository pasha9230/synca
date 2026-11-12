"""Деление тестов на части для .github/workflows/tests.yml: по файлам, с весами по времени.

Деление детерминированное: файлы tests/**/test_*.py и tests/**/*_test.py раскладываются жадно (самый долгий файл
в самую лёгкую часть) по весам из .github/test-durations.json (секунды файла на машине GitHub). Файл без веса
получает медиану известных весов. Что каждый тест попал ровно в одну часть, проверяет задача split в tests.yml
(списки --collect-only всех тестов и частей совпадают).

    python .github/split_tests.py list K N        файлы части K из N, по одному в строке
    python .github/split_tests.py plan N          вес и число файлов каждой части
    python .github/split_tests.py weights a.xml…  секунды по файлам из junit (-o junit_family=xunit1), JSON

Обновить веса: из журналов частей (шаг «время по файлам») собрать JSON в .github/test-durations.json.
"""
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = ROOT / ".github" / "test-durations.json"


def test_files():
    found = set()
    for pattern in ("test_*.py", "*_test.py"):
        for p in (ROOT / "tests").rglob(pattern):
            if p.is_file():
                found.add(p.relative_to(ROOT).as_posix())
    return sorted(found)


def load_weights():
    if not WEIGHTS.exists():
        return {}
    data = json.loads(WEIGHTS.read_text(encoding="utf-8"))
    return {str(k): float(v) for k, v in data.items()}


def shards(n, files=None, weights=None):
    if n < 1:
        raise ValueError("число частей от 1")
    files = test_files() if files is None else sorted(files)
    weights = load_weights() if weights is None else weights
    known = [weights[f] for f in files if f in weights]
    default = statistics.median(known) if known else 1.0
    weight = {f: weights.get(f, default) for f in files}
    bins = [[0.0, i, []] for i in range(n)]
    for f in sorted(files, key=lambda f: (-weight[f], f)):
        b = min(bins, key=lambda b: (b[0], b[1]))
        b[0] += weight[f]
        b[2].append(f)
    return [(b[0], sorted(b[2])) for b in bins]


def junit_weights(paths):
    total = {}
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            f = case.get("file")
            if f:
                total[f] = total.get(f, 0.0) + float(case.get("time") or 0)
    return {f: round(t, 2) for f, t in sorted(total.items())}


def main(argv):
    if len(argv) == 3 and argv[0] == "list":
        k, n = int(argv[1]), int(argv[2])
        if not 1 <= k <= n:
            raise SystemExit(f"часть {k} вне 1..{n}")
        print("\n".join(shards(n)[k - 1][1]))
    elif len(argv) == 2 and argv[0] == "plan":
        for i, (w, files) in enumerate(shards(int(argv[1])), 1):
            print(f"часть {i}: {w:.0f} с, файлов {len(files)}")
    elif len(argv) >= 2 and argv[0] == "weights":
        print(json.dumps(junit_weights(argv[1:]), ensure_ascii=False, sort_keys=True))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
