"""The engine's test coverage, held to a floor per module (PLAN 0.23.3).

    python packaging/coverage.py            # run the tests with coverage; exit 1 below a floor
    python packaging/coverage.py --write    # set the floors to today's numbers (rounded down to 5 %)

Plain `pytest` stays as fast as before: coverage runs only here. Floors live in
packaging/coverage-floors.json ({"total": n, "modules": {"bakin/x.py": n}}); a module that isn't
listed (a new one) only counts toward the total. Raise a floor when its tests grow; lower one only
on purpose, in the same change that explains why.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLOORS = ROOT / "packaging" / "coverage-floors.json"
STEP = 5


def measure() -> dict:
    """{"total": percent, "modules": {"bakin/x.py": percent}} from a full test run."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "coverage.json"
        run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--cov=bakin",
                              "--cov-report=", f"--cov-report=json:{report}", "tests"], cwd=ROOT,
                             env={**os.environ, "COVERAGE_FILE": str(Path(tmp) / ".coverage")})  # no data file left in the repo
        if run.returncode != 0:
            sys.exit(f"the tests failed (exit {run.returncode}): coverage not checked")
        data = json.loads(report.read_text(encoding="utf-8"))
    modules = {Path(name).as_posix(): f["summary"]["percent_covered"] for name, f in data["files"].items()}
    return {"total": data["totals"]["percent_covered"], "modules": modules}


def floor(percent: float) -> int:
    return int(percent // STEP * STEP)


def check(now: dict, floors: dict) -> list[str]:
    """What dropped below its floor (empty when all is well)."""
    low = []
    if now["total"] < floors.get("total", 0):
        low.append(f"total: {now['total']:.1f} % < {floors['total']} %")
    for name, want in sorted(floors.get("modules", {}).items()):
        got = now["modules"].get(name)
        if got is None:
            low.append(f"{name}: not measured (moved or removed? update the floors)")
        elif got < want:
            low.append(f"{name}: {got:.1f} % < {want} %")
    return low


def main(argv: list[str]) -> int:
    now = measure()
    width = max(map(len, now["modules"]))
    for name, pct in sorted(now["modules"].items(), key=lambda kv: kv[1]):
        print(f"  {name:<{width}}  {pct:5.1f} %")
    print(f"  {'total':<{width}}  {now['total']:5.1f} %")
    if "--write" in argv:
        floors = {"total": floor(now["total"]), "modules": {n: floor(p) for n, p in sorted(now["modules"].items())}}
        FLOORS.write_text(json.dumps(floors, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"floors written to {FLOORS.relative_to(ROOT).as_posix()}")
        return 0
    low = check(now, json.loads(FLOORS.read_text(encoding="utf-8")))
    for line in low:
        print(f"BELOW THE FLOOR  {line}")
    print("coverage OK" if not low else f"{len(low)} below the floor")
    return 1 if low else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
