"""플랫폼 시험의 분기 커버리지로 다른 시험이 이미 지키는 시험을 찾고, 시험을 줄인 뒤 잃은 분기를 센다 (P83).

입력은 시험마다 문맥을 남긴 커버리지 파일이다. platform/ 에서 시험용 DB 주소를 셸에 두고 만든다:

    COVERAGE_FILE=<out>/.coverage uv run --with pytest-cov pytest -q -n auto \\
        --cov=daegam --cov-branch --cov-context=test --cov-report=

    python3 -I scripts/test-redundancy.py candidates <.coverage> <ids.txt> <durations.log>
        ids.txt 에 적힌 시험(한 줄에 시험 id 하나) 가운데, 그 시험이 지나는 분기를 모두 남은 다른 시험(목록 밖의 시험
        포함)이 지나는 것을 오래 걸리는 것부터 고른다. durations.log 는 pytest --durations=0 의 출력이다
    python3 -I scripts/test-redundancy.py compare <before .coverage> <after .coverage>
        before 의 시험 전체가 지나던 분기 가운데 after 의 시험 전체가 지나지 않는 것을 파일별로 센다. 하나라도 있으면 1 이다

시험의 setup·teardown 문맥은 세지 않는다. 고정물이 지나는 분기는 시험마다 같아 무엇을 지키는지 말해 주지 않는다.
"""

from __future__ import annotations

import collections
import json
import re
import sqlite3
import sys

Arc = tuple[str, int, int]


def runs(path: str) -> dict[str, set[Arc]]:
    """시험 id -> 그 시험의 run 문맥이 지난 분기 (파일 경로, 시작 줄, 끝 줄)."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    files = dict(db.execute("select id, path from file"))
    contexts = {cid: c[: -len("|run")] for cid, c in db.execute("select id, context from context") if c.endswith("|run")}
    out: dict[str, set[Arc]] = collections.defaultdict(set)
    for fid, cid, a, b in db.execute("select file_id, context_id, fromno, tono from arc"):
        test = contexts.get(cid)
        if test is not None:
            out[test].add((files[fid].split("/daegam/", 1)[-1], a, b))
    return out


def durations(path: str) -> dict[str, float]:
    out: dict[str, float] = collections.defaultdict(float)
    for line in open(path, encoding="utf-8"):
        m = re.match(r"([\d.]+)s (?:setup|call|teardown)\s+(\S+)", line)
        if m:
            out[m.group(2)] += float(m.group(1))
    return out


def candidates(cov_path: str, ids_path: str, dur_path: str) -> int:
    cov = runs(cov_path)
    ids = [line.strip() for line in open(ids_path, encoding="utf-8") if line.strip()]
    dur = durations(dur_path)
    counts: collections.Counter[Arc] = collections.Counter(a for arcs in cov.values() for a in arcs)
    removable = []
    for test in sorted(ids, key=lambda t: -dur.get(t, 0.0)):
        arcs = cov.get(test, set())
        if all(counts[a] > 1 for a in arcs):
            removable.append(test)
            for a in arcs:
                counts[a] -= 1
    json.dump(sorted(removable), sys.stdout, indent=0)
    print(f"\n{len(removable)} of {len(ids)} can go with no branch lost", file=sys.stderr)
    return 0


def compare(before_path: str, after_path: str) -> int:
    """잃은 분기를 둘로 나눈다. 그 분기를 지나던 시험이 모두 사라졌으면 잃은 것이고, 그 가운데 남은 시험이 있으면
    그 시험이 이번 실행에서 그 분기를 지나지 않은 것이다(배경 작업의 시점에 따라 지나기도 하는 분기). 앞의 것만
    실패로 본다."""
    b, a = runs(before_path), runs(after_path)
    before, after = set().union(*b.values()), set().union(*a.values())
    by_arc: dict[Arc, list[str]] = collections.defaultdict(list)
    for test, arcs in b.items():
        for arc in arcs - after:
            by_arc[arc].append(test)
    lost = collections.Counter(arc[0] for arc, tests in by_arc.items() if not any(t in a for t in tests))
    unstable = sum(1 for tests in by_arc.values() if any(t in a for t in tests))
    for path, n in lost.most_common():
        print(f"{n:5d} {path}")
    print(
        f"branches: before {len(before)}, after {len(after)}, lost {sum(lost.values())}, "
        f"not reached this run by a test that still exists {unstable}"
    )
    return 1 if lost else 0


def main(argv: list[str]) -> int:
    if len(argv) == 5 and argv[1] == "candidates":
        return candidates(*argv[2:])
    if len(argv) == 4 and argv[1] == "compare":
        return compare(*argv[2:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
