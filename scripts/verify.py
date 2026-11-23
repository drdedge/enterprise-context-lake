"""Master offline verification runner executing all 7 external oracles.

Runs 100% offline with zero external LLM/cloud API keys required:
  - Pre-check: fixtures/validate_fixtures.py + LF line-ending check
  - Oracle 1: tests/test_bitemporal_replay.py
  - Oracle 2: tests/test_security_redteam.py
  - Oracle 3: tests/test_adapter_conformance.py
  - Oracle 4: tests/test_domain_workflows.py
  - Oracle 5: tests/test_knowledge_factory.py
  - Oracle 6: evals/eval_mcp_vs_codemode.py
  - Oracle 7: tests/e2e/test_ui_playwright.spec.ts (live FastAPI + Vite + Chromium)

Saves machine-readable summary to data/local/verify_summary.json and prints a
clean Markdown verification table to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from dev import (  # noqa: E402
    ProcessTreeManager,
    build_backend_env,
    is_port_open,
    resolve_npx_cmd,
    resolve_venv_python,
    start_backend,
    start_frontend,
    wait_for_http,
)

REQUIRED_SCREENSHOTS = [
    "docs/screenshots/home.png",
    "docs/screenshots/search-cmdk.png",
    "docs/screenshots/graph-funds.png",
    "docs/screenshots/graph-reverse-topic.png",
    "docs/screenshots/inspector-conflict-redacted.png",
    "docs/screenshots/personal-memory.png",
    "docs/screenshots/manual-upload-triage.png",
]

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

PYTEST_ORACLES: list[tuple[int, str, str]] = [
    (1, "Oracle 1: Bitemporal Replay & SDK Isolation", "tests/test_bitemporal_replay.py"),
    (2, "Oracle 2: Security Red-Team (9 Surfaces)", "tests/test_security_redteam.py"),
    (3, "Oracle 3: Hexagonal Adapter Conformance", "tests/test_adapter_conformance.py"),
    (4, "Oracle 4: Institutional Domain Workflows", "tests/test_domain_workflows.py"),
    (5, "Oracle 5: Knowledge Factory & DeepAgents", "tests/test_knowledge_factory.py"),
]


def _parse_pytest_counts(output: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    for key, pattern in (
        ("passed", r"(\d+)\s+passed"),
        ("failed", r"(\d+)\s+failed"),
        ("skipped", r"(\d+)\s+skipped"),
        ("errors", r"(\d+)\s+error"),
    ):
        matches = re.findall(pattern, output)
        if matches:
            counts[key] = int(matches[-1])
    return counts


def _run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: int = 300,
) -> tuple[int, str, float]:
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return proc.returncode, proc.stdout, elapsed_ms


def check_lf_hygiene() -> dict[str, Any]:
    """Verify LF-only endings across source/docs/fixtures/tests/scripts/evals."""
    text_exts = {".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".md", ".yml", ".yaml", ".csv", ".css", ".html", ".ps1", ".toml", ".sql"}
    scan_roots = ["packages", "apps/web/src", "fixtures", "scripts", "tests", "evals", "docs/adr", "docs/runbooks"]
    crlf_files: list[str] = []
    scanned = 0
    for rel_root in scan_roots:
        root = REPO_ROOT / rel_root
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            parts = set(path.parts)
            if parts & {"node_modules", ".venv", "__pycache__", "dist", "playwright-report", "test-results"}:
                continue
            if path.suffix.lower() not in text_exts:
                continue
            scanned += 1
            if b"\r\n" in path.read_bytes():
                crlf_files.append(path.relative_to(REPO_ROOT).as_posix())
    return {
        "name": "LF Line-Ending Hygiene",
        "passed": len(crlf_files) == 0,
        "files_scanned": scanned,
        "crlf_files": crlf_files,
    }


def run_fixture_validation(py_exe: Path, env: dict[str, str]) -> dict[str, Any]:
    rc, out, elapsed_ms = _run_subprocess(
        [str(py_exe), str(REPO_ROOT / "fixtures" / "validate_fixtures.py")],
        cwd=REPO_ROOT,
        env=env,
        timeout_s=60,
    )
    return {
        "name": "Fixture Integrity & Policy Matrix",
        "passed": rc == 0,
        "exit_code": rc,
        "duration_ms": elapsed_ms,
        "output_tail": out[-1500:],
    }


def run_pytest_oracle(
    oracle_num: int,
    name: str,
    rel_test_path: str,
    py_exe: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    test_path = REPO_ROOT / rel_test_path
    if not test_path.is_file():
        return {
            "oracle": oracle_num,
            "name": name,
            "target": rel_test_path,
            "passed": False,
            "exit_code": 2,
            "counts": {"passed": 0, "failed": 0, "skipped": 0, "errors": 1},
            "duration_ms": 0.0,
            "output_tail": f"Missing test file: {rel_test_path}",
        }

    rc, out, elapsed_ms = _run_subprocess(
        [str(py_exe), "-m", "pytest", "-v", "--tb=short", rel_test_path],
        cwd=REPO_ROOT,
        env=env,
        timeout_s=300,
    )
    counts = _parse_pytest_counts(out)
    strict_pass = (
        rc == 0
        and counts["passed"] > 0
        and counts["failed"] == 0
        and counts["errors"] == 0
        and counts["skipped"] == 0
    )
    return {
        "oracle": oracle_num,
        "name": name,
        "target": rel_test_path,
        "passed": strict_pass,
        "exit_code": rc,
        "counts": counts,
        "duration_ms": elapsed_ms,
        "output_tail": out[-3000:],
    }


def run_eval_oracle(py_exe: Path, env: dict[str, str]) -> dict[str, Any]:
    rel_path = "evals/eval_mcp_vs_codemode.py"
    rc, out, elapsed_ms = _run_subprocess(
        [str(py_exe), rel_path],
        cwd=REPO_ROOT,
        env=env,
        timeout_s=300,
    )
    eval_json_path = REPO_ROOT / "data" / "local" / "eval_mcp_vs_codemode.json"
    metrics: dict[str, Any] | None = None
    if eval_json_path.is_file():
        try:
            metrics = json.loads(eval_json_path.read_text(encoding="utf-8"))
        except Exception:
            metrics = None
    return {
        "oracle": 6,
        "name": "Oracle 6: Raw FastMCP vs Code Mode Eval",
        "target": rel_path,
        "passed": rc == 0 and metrics is not None,
        "exit_code": rc,
        "duration_ms": elapsed_ms,
        "metrics_summary": (metrics or {}).get("summary"),
        "output_tail": out[-3000:],
    }


def _verify_screenshots() -> tuple[bool, list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    all_ok = True
    for rel in REQUIRED_SCREENSHOTS:
        p = REPO_ROOT / rel
        if not p.is_file():
            results.append({"path": rel, "exists": False, "bytes": 0, "valid_png": False})
            all_ok = False
            continue
        raw = p.read_bytes()
        valid_png = len(raw) > 2048 and raw.startswith(PNG_SIGNATURE)
        if not valid_png:
            all_ok = False
        results.append(
            {
                "path": rel,
                "exists": True,
                "bytes": len(raw),
                "valid_png": valid_png,
            }
        )
    return all_ok, results


def run_playwright_oracle(
    env: dict[str, str],
    *,
    api_port: int = 8000,
    web_port: int = 5173,
) -> dict[str, Any]:
    """Boot live FastAPI + Vite servers, run Playwright Chromium suite, and tear down cleanly."""
    e2e_dir = REPO_ROOT / "tests" / "e2e"
    log_dir = REPO_ROOT / "data" / "local"
    log_dir.mkdir(parents=True, exist_ok=True)
    backend_log = log_dir / "verify_backend.log"
    frontend_log = log_dir / "verify_frontend.log"
    db_path = log_dir / f"verify_e2e_{os.getpid()}.sqlite3"

    manager = ProcessTreeManager()
    t0 = time.perf_counter()
    rc = 1
    out = ""
    backend_fh = None
    frontend_fh = None

    try:
        if is_port_open("127.0.0.1", api_port):
            raise RuntimeError(f"Port {api_port} is already occupied before E2E boot")
        if is_port_open("127.0.0.1", web_port):
            raise RuntimeError(f"Port {web_port} is already occupied before E2E boot")

        backend_fh = backend_log.open("w", encoding="utf-8")
        frontend_fh = frontend_log.open("w", encoding="utf-8")

        backend_proc = start_backend(
            manager,
            host="127.0.0.1",
            port=api_port,
            offline=True,
            db_path=db_path,
            stdout=backend_fh,
            stderr=subprocess.STDOUT,
        )
        wait_for_http(
            [
                f"http://127.0.0.1:{api_port}/healthz",
                f"http://127.0.0.1:{api_port}/api/v1/health",
            ],
            timeout_s=60.0,
            proc=backend_proc,
        )

        frontend_proc = start_frontend(
            manager,
            host="127.0.0.1",
            port=web_port,
            api_port=api_port,
            stdout=frontend_fh,
            stderr=subprocess.STDOUT,
        )
        wait_for_http(
            [f"http://127.0.0.1:{web_port}/"],
            timeout_s=60.0,
            proc=frontend_proc,
        )
        wait_for_http(
            [f"http://127.0.0.1:{web_port}/healthz"],
            timeout_s=30.0,
            proc=frontend_proc,
        )

        pw_env = dict(env)
        pw_env["ECL_EXTERNAL_SERVERS"] = "1"
        pw_env["ECL_API_URL"] = f"http://127.0.0.1:{api_port}"
        pw_env["ECL_WEB_URL"] = f"http://127.0.0.1:{web_port}"

        rc, out, _ = _run_subprocess(
            [resolve_npx_cmd(), "playwright", "test", "--reporter=list"],
            cwd=e2e_dir,
            env=pw_env,
            timeout_s=300,
        )
        (log_dir / "verify_playwright.log").write_text(out, encoding="utf-8", newline="\n")
    except Exception as exc:
        rc = 1
        out = f"Playwright harness startup error: {exc}\n"
        if backend_log.is_file():
            out += "\n--- backend log tail ---\n" + backend_log.read_text(encoding="utf-8", errors="replace")[-1500:]
        if frontend_log.is_file():
            out += "\n--- frontend log tail ---\n" + frontend_log.read_text(encoding="utf-8", errors="replace")[-1500:]
    finally:
        manager.shutdown_all()
        if backend_fh is not None:
            backend_fh.close()
        if frontend_fh is not None:
            frontend_fh.close()
        # Brief settle window and verify ports are released
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and (
            is_port_open("127.0.0.1", api_port) or is_port_open("127.0.0.1", web_port)
        ):
            time.sleep(0.25)
        if db_path.exists():
            try:
                db_path.unlink()
            except OSError:
                pass
        pw_debug_log = e2e_dir / "debug.log"
        if pw_debug_log.exists():
            try:
                pw_debug_log.unlink()
            except OSError:
                pass

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    ports_clean = (not is_port_open("127.0.0.1", api_port)) and (not is_port_open("127.0.0.1", web_port))
    screenshots_ok, screenshots = _verify_screenshots()

    return {
        "oracle": 7,
        "name": "Oracle 7: Playwright E2E, WCAG 2.2 AA & Screenshots",
        "target": "tests/e2e/test_ui_playwright.spec.ts",
        "passed": rc == 0 and screenshots_ok and ports_clean,
        "exit_code": rc,
        "duration_ms": elapsed_ms,
        "ports_clean_after_teardown": ports_clean,
        "screenshots_ok": screenshots_ok,
        "screenshots": screenshots,
        "output_tail": out[-3000:],
    }


def render_markdown_summary(summary: dict[str, Any]) -> str:
    lines = [
        "# Enterprise Context Lake — Verification Summary",
        "",
        f"- **Timestamp (UTC):** `{summary['timestamp']}`",
        f"- **Offline Mode:** `{summary['offline_mode']}` (zero external LLM keys)",
        f"- **Overall Status:** `{'PASS' if summary['all_passed'] else 'FAIL'}`",
        "",
        "| Check / Oracle | Status | Details | Duration (ms) |",
        "|---|---|---|---|",
    ]
    for pre in summary["prechecks"]:
        status = "PASS" if pre["passed"] else "FAIL"
        detail = (
            f"files={pre['files_scanned']}, crlf={len(pre['crlf_files'])}"
            if "files_scanned" in pre
            else f"exit={pre.get('exit_code', 0)}"
        )
        lines.append(f"| {pre['name']} | {status} | {detail} | {pre.get('duration_ms', 0)} |")

    for item in summary["oracles"]:
        status = "PASS" if item["passed"] else "FAIL"
        if "counts" in item:
            c = item["counts"]
            detail = f"{c['passed']} passed, {c['failed']} failed, {c['skipped']} skipped"
        elif item["oracle"] == 6 and item.get("metrics_summary"):
            ms = item["metrics_summary"]
            detail = (
                f"queries={ms.get('query_count')}, "
                f"raw_f1={ms.get('raw_mcp', {}).get('avg_entity_f1')}, "
                f"cm_f1={ms.get('codemode_mcp', {}).get('avg_entity_f1')}, "
                f"escapes={ms.get('sandbox_escapes')}"
            )
        elif item["oracle"] == 7:
            ok_shots = sum(1 for s in item.get("screenshots", []) if s.get("valid_png"))
            detail = f"screenshots={ok_shots}/{len(REQUIRED_SCREENSHOTS)}, ports_clean={item.get('ports_clean_after_teardown')}"
        else:
            detail = f"exit={item.get('exit_code')}"
        lines.append(f"| {item['name']} | {status} | {detail} | {item['duration_ms']} |")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run all 7 Enterprise Context Lake verification oracles.")
    parser.add_argument(
        "--oracles",
        default="1,2,3,4,5,6,7",
        help="Comma-separated list of oracle numbers to run (default: 1,2,3,4,5,6,7).",
    )
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=5173)
    args = parser.parse_args(argv)

    selected = {int(tok.strip()) for tok in args.oracles.split(",") if tok.strip()}
    py_exe = resolve_venv_python()
    offline_env = build_backend_env(llm_mode="deterministic", offline=True)

    prechecks = [
        check_lf_hygiene(),
        run_fixture_validation(py_exe, offline_env),
    ]

    oracle_results: list[dict[str, Any]] = []
    for num, name, rel_path in PYTEST_ORACLES:
        if num in selected:
            print(f"\n>>> Running {name} ({rel_path})...", flush=True)
            res = run_pytest_oracle(num, name, rel_path, py_exe, offline_env)
            oracle_results.append(res)
            print(f"    -> {'PASS' if res['passed'] else 'FAIL'} ({res['counts']}, {res['duration_ms']}ms)", flush=True)
            if not res["passed"]:
                print(res["output_tail"], flush=True)

    if 6 in selected:
        print("\n>>> Running Oracle 6: Raw FastMCP vs Code Mode Eval...", flush=True)
        res6 = run_eval_oracle(py_exe, offline_env)
        oracle_results.append(res6)
        print(f"    -> {'PASS' if res6['passed'] else 'FAIL'} ({res6['duration_ms']}ms)", flush=True)
        if not res6["passed"]:
            print(res6["output_tail"], flush=True)

    if 7 in selected:
        print("\n>>> Running Oracle 7: Playwright E2E, WCAG 2.2 AA & Screenshots...", flush=True)
        res7 = run_playwright_oracle(offline_env, api_port=args.api_port, web_port=args.web_port)
        oracle_results.append(res7)
        print(f"    -> {'PASS' if res7['passed'] else 'FAIL'} ({res7['duration_ms']}ms)", flush=True)
        if not res7["passed"]:
            print(res7["output_tail"], flush=True)

    all_passed = all(p["passed"] for p in prechecks) and all(o["passed"] for o in oracle_results)
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "offline_mode": True,
        "selected_oracles": sorted(selected),
        "all_passed": all_passed,
        "prechecks": prechecks,
        "oracles": oracle_results,
    }

    out_dir = REPO_ROOT / "data" / "local"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "verify_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")

    md = render_markdown_summary(summary)
    print("\n" + md, flush=True)
    print(f"Saved JSON summary to {summary_path.relative_to(REPO_ROOT).as_posix()}", flush=True)
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
