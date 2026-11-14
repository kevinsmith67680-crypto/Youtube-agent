"""Record build-level verification evidence for the readiness scorecard.

Writes ``docs/benchmarks/phase8_verification.json``. Fast checks are executed here: ruff,
mypy (src, tests strict), TypeScript, bandit, pip-audit and npm audit when those tools are
available. Slow results are parsed from the logs the runs produced; every summary quotes
the log, and nothing is entered by hand. A check whose evidence is missing is recorded as
not passed.

    python scripts/record_verification.py \\
        --pytest-log py312.log --pytest-log py313.log --e2e-log e2e.log \\
        --vitest-log vitest.log --playwright-log playwright.log \\
        --deployment deployment_checks.json --evaluation docs/benchmarks/phase8_evaluation.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = re.compile(r"=*\s*((?:\d+ \w+(?:, )?)+) in [\d.]+s")


def run(cmd: list[str], cwd: Path = ROOT, timeout: int = 900) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{cmd[0]}: {exc}"
    tail = (p.stdout + p.stderr).strip().splitlines()[-1:] or [""]
    return p.returncode == 0, tail[0][:200]


def pytest_result(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"passed": False, "summary": "log missing"}
    text = path.read_text(errors="replace")
    found = list(SUMMARY.finditer(text))
    m = found[-1] if found else None
    if m is None:
        return {"passed": False, "summary": f"no pytest summary in {path.name}"}
    summary = m.group(1)
    bad = re.search(r"\b(\d+) (failed|error|errors)\b", summary)
    cov = re.search(r"^TOTAL\s+.*?(\d+)%\s*$", text, re.M)
    out = {"passed": bad is None, "summary": summary.strip(), "log": path.name}
    if cov:
        out["coverage_percent"] = int(cov.group(1))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pytest-log", type=Path, action="append", default=[])
    ap.add_argument("--e2e-log", type=Path)
    ap.add_argument("--vitest-log", type=Path)
    ap.add_argument("--playwright-log", type=Path)
    ap.add_argument("--next-build-log", type=Path)
    ap.add_argument("--deployment", type=Path)
    ap.add_argument("--migration-log", type=Path)
    ap.add_argument("--evaluation", type=Path)
    ap.add_argument("--secvenv", type=Path, help="virtualenv with pip-audit, bandit")
    ap.add_argument("--out", type=Path, default=ROOT / "docs/benchmarks/phase8_verification.json")
    args = ap.parse_args()

    ev: dict[str, Any] = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commit": run(["git", "rev-parse", "--short", "HEAD"])[1],
    }
    runs = [pytest_result(p) for p in args.pytest_log]
    ev["backend_tests"] = {
        "passed": bool(runs) and all(r["passed"] for r in runs),
        "summary": "; ".join(
            f"{r.get('log')}: {r['summary']}"
            + (f" (coverage {r['coverage_percent']}%)" if "coverage_percent" in r else "")
            for r in runs
        )
        or "no runs",
    }
    ev["e2e_scenarios"] = pytest_result(args.e2e_log)
    py = str(ROOT / ".venv/bin")
    checks = {
        "ruff": run([f"{py}/ruff", "check", "src", "tests"]),
        "ruff_format": run([f"{py}/ruff", "format", "--check", "src", "tests"]),
        "mypy_src": run([f"{py}/mypy", "src"]),
        "mypy_tests_strict": run([f"{py}/mypy", "tests", "--strict"]),
    }
    npx = shutil.which("npx")
    if npx:
        checks["tsc"] = run([npx, "tsc", "--noEmit"], cwd=ROOT / "frontend")
    ev["static_checks"] = {
        "passed": all(ok for ok, _ in checks.values()),
        "summary": "; ".join(
            f"{k}: {'ok' if ok else 'FAIL ' + msg}" for k, (ok, msg) in checks.items()
        ),
    }
    fe = []
    for name, path, pattern in (
        ("vitest", args.vitest_log, r"Tests\s+(\d+ passed[^\n]*)"),
        ("playwright", args.playwright_log, r"(\d+ passed[^\n]*)"),
        ("next build", args.next_build_log, r"(BUILD_EXIT=\d+)"),
    ):
        if path is None or not path.exists():
            fe.append((name, False, "log missing"))
            continue
        text = path.read_text(errors="replace")
        m = re.findall(pattern, text)
        failed = re.search(r"\b\d+ failed\b", text) or (
            name == "next build" and "BUILD_EXIT=0" not in text
        )
        fe.append((name, bool(m) and not failed, m[-1] if m else "no result"))
    ev["frontend_tests"] = {
        "passed": all(ok for _, ok, _ in fe),
        "summary": "; ".join(f"{n}: {s}" for n, _, s in fe),
    }
    sec: list[tuple[str, bool, str]] = []
    if args.secvenv:
        ok, msg = run(
            [str(args.secvenv / "bin/bandit"), "-q", "-r", "src", "--severity-level", "medium"]
        )
        sec.append(("bandit (medium+)", ok, "no medium/high findings" if ok else msg))
        req = Path("/tmp/phase8_requirements.txt")  # noqa: S108
        uv = shutil.which("uv")
        if uv:
            out = subprocess.run(
                [uv, "pip", "freeze", "--python", str(ROOT / ".venv/bin/python")],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            keep = [
                x
                for x in out.splitlines()
                if not x.startswith("-e") and "youtube-trend-agent" not in x
            ]
            req.write_text("\n".join(keep))
            ok, msg = run(
                [
                    str(args.secvenv / "bin/pip-audit"),
                    "-r",
                    str(req),
                    "--no-deps",
                    "--progress-spinner",
                    "off",
                ]
            )
            sec.append(("pip-audit", ok, msg))
        ok, msg = run(
            [
                str(args.secvenv / "bin/detect-secrets-hook"),
                "--baseline",
                ".secrets.baseline",
                *subprocess.run(
                    ["git", "ls-files"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.split(),
            ]
        )
        sec.append(("detect-secrets (vs baseline)", ok, "no new secrets" if ok else msg))
    if npx:
        npm = shutil.which("npm") or "npm"
        ok, msg = run([npm, "audit", "--omit=dev", "--audit-level=low"], cwd=ROOT / "frontend")
        sec.append(("npm audit --omit=dev", ok, msg))
    ev["dependency_audit"] = {
        "passed": bool(sec) and all(ok for n, ok, _ in sec if "audit" in n),
        "summary": "; ".join(f"{n}: {m}" for n, ok, m in sec if "audit" in n) or "not run",
    }
    ev["code_security_scan"] = {
        "passed": bool(sec) and all(ok for n, ok, _ in sec if "audit" not in n),
        "summary": "; ".join(f"{n}: {m}" for n, ok, m in sec if "audit" not in n) or "not run",
    }
    if args.deployment and args.deployment.exists():
        d = json.loads(args.deployment.read_text())
        b, f = d.get("backend_image", {}), d.get("frontend_image", {})
        ok = (
            b.get("built")
            and f.get("built")
            and b.get("api_health_ready") == 200
            and b.get("production_guard_exit_code") == 2
            and f.get("production_without_credentials") == 503
            and f.get("anonymous") == 401
            and f.get("correct_password") == 200
        )
        ev["deployment_artifacts"] = {
            "passed": bool(ok),
            "summary": (
                f"backend image built ({b.get('build_args')}), uid {b.get('user_uid')}, ready "
                f"{b.get('api_health_ready')}, production guard exit "
                f"{b.get('production_guard_exit_code')}; frontend uid {f.get('user_uid')}, "
                f"unconfigured {f.get('production_without_credentials')}, "
                f"anonymous {f.get('anonymous')}, authenticated {f.get('correct_password')}; "
                f"compose {d.get('compose')}"
            ),
        }
    else:
        ev["deployment_artifacts"] = {"passed": False, "summary": "no deployment check record"}
    if args.migration_log and args.migration_log.exists():
        text = args.migration_log.read_text()
        ok = "MIGRATIONS_OK" in text
        ev["migration_validation"] = {"passed": ok, "summary": text.strip().splitlines()[-1][:200]}
    else:
        ev["migration_validation"] = {"passed": False, "summary": "log missing"}
    if args.evaluation and args.evaluation.exists():
        e = json.loads(args.evaluation.read_text())
        card = e["scorecard"]
        offline = {k: v["offline"] for k, v in card["components"].items()}
        ok = (
            all(v == "MEETS_TARGET" for v in offline.values())
            and card["pipeline_integrity"] == "PASS"
        )
        ev["offline_evaluation"] = {
            "passed": ok,
            "summary": f"offline {offline}; integrity "
            f"{card['pipeline_integrity']}; real world {card['real_world_accuracy']} "
            f"(synthetic data; {e['generated_at']})",
        }
    else:
        ev["offline_evaluation"] = {"passed": False, "summary": "no evaluation report"}
    args.out.write_text(json.dumps(ev, indent=2) + "\n")
    print(
        json.dumps(
            {k: v.get("passed") if isinstance(v, dict) else v for k, v in ev.items()}, indent=1
        )
    )


if __name__ == "__main__":
    main()
