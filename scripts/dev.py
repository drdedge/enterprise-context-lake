"""Cross-platform local development runner for Enterprise Context Lake.

Starts:
  1. FastAPI + FastMCP backend (Uvicorn on 127.0.0.1:8000)
  2. React + Vite frontend (npm.cmd run dev on 127.0.0.1:5173)

Guarantees clean Windows process-tree teardown (CREATE_NEW_PROCESS_GROUP +
taskkill /F /T /PID) so ports 8000 and 5173 are never left orphaned.
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import importlib
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_optional_dotenv(repo_root: Path = REPO_ROOT) -> None:
    """Load .env from repo root without overwriting existing environment vars."""
    env_path = repo_root / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=env_path, override=False)
    except Exception:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def resolve_venv_python(repo_root: Path = REPO_ROOT) -> Path:
    """Return the workspace .venv Python executable if available."""
    if os.name == "nt":
        candidate = repo_root / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = repo_root / ".venv" / "bin" / "python"
    return candidate if candidate.is_file() else Path(sys.executable)


def resolve_npm_cmd() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def resolve_npx_cmd() -> str:
    return "npx.cmd" if os.name == "nt" else "npx"


def resolve_uvicorn_target(repo_root: Path = REPO_ROOT) -> tuple[str, bool]:
    """Detect the FastAPI ASGI module/callable exported by packages/api.

    Returns (target_spec, is_factory).
    """
    pkg_roots = [
        repo_root / "packages" / name
        for name in ("core", "adapters", "api", "mcp", "sdk", "agents")
    ]
    for p in pkg_roots:
        p_str = str(p)
        if p.is_dir() and p_str not in sys.path:
            sys.path.insert(0, p_str)

    candidates: list[tuple[str, str, bool]] = [
        ("context_api.main", "app", False),
        ("context_api.app", "create_app", True),
        ("context_api", "create_app", True),
        ("context_api.app", "app", False),
        ("context_api", "app", False),
    ]
    for mod_name, attr_name, is_factory in candidates:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        if hasattr(mod, attr_name):
            return f"{mod_name}:{attr_name}", is_factory
    return "context_api.main:create_app", True


def is_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def wait_for_http(
    urls: list[str],
    *,
    timeout_s: float = 45.0,
    proc: subprocess.Popen[Any] | None = None,
) -> str:
    """Poll one or more loopback URLs (bypassing proxies) until HTTP 2xx arrives."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout_s
    last_err: str = "no response"
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(
                f"Child process exited prematurely with code {proc.returncode} while waiting for {urls}"
            )
        for url in urls:
            try:
                req = urllib.request.Request(url, method="GET")
                with opener.open(req, timeout=2.0) as resp:
                    if 200 <= resp.status < 300:
                        return url
                    last_err = f"HTTP {resp.status} from {url}"
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code} from {url}"
            except Exception as exc:
                last_err = f"{url}: {exc}"
        time.sleep(0.25)
    raise TimeoutError(f"Timed out after {timeout_s:.1f}s waiting for {urls} (last: {last_err})")


def _create_windows_kill_on_close_job() -> int | None:
    """Create a Win32 Job Object configured with KILL_ON_JOB_CLOSE for atomic tree teardown."""
    if os.name != "nt":
        return None
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x2000) | BREAKAWAY_OK (0x0800)
        info.BasicLimitInformation.LimitFlags = 0x2000
        JobObjectExtendedLimitInformation = 9
        k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k32.SetInformationJobObject.restype = ctypes.c_int
        k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info))
        return int(job)
    except Exception:
        return None


def _assign_to_windows_job(job: int | None, proc: subprocess.Popen[Any]) -> bool:
    if os.name != "nt" or not job:
        return False
    try:
        k32 = ctypes.windll.kernel32
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.AssignProcessToJobObject.restype = ctypes.c_int
        return bool(k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(int(proc._handle))))  # type: ignore[attr-defined]
    except Exception:
        return False


def _terminate_windows_job(job: int | None) -> bool:
    if os.name != "nt" or not job:
        return False
    try:
        k32 = ctypes.windll.kernel32
        k32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.TerminateJobObject.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        ok = bool(k32.TerminateJobObject(ctypes.c_void_p(job), 1))
        k32.CloseHandle(ctypes.c_void_p(job))
        return ok
    except Exception:
        return False


def kill_process_tree(
    proc: subprocess.Popen[Any] | None,
    *,
    job_handle: int | None = None,
    grace_s: float = 3.0,
) -> None:
    """Terminate a child process and its entire descendant tree on Windows and POSIX."""
    if os.name == "nt" and job_handle:
        _terminate_windows_job(job_handle)

    if proc is None:
        return
    if proc.poll() is not None:
        return

    pid = proc.pid
    if os.name == "nt":
        try:
            proc.kill()
        except Exception:
            pass
        if proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=3,
                )
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    try:
        proc.wait(timeout=grace_s)
    except Exception:
        pass


def spawn_process_group(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout: Any = None,
    stderr: Any = None,
) -> subprocess.Popen[Any]:
    """Spawn a subprocess in its own process group for deterministic tree cleanup."""
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdout": stdout,
        "stderr": stderr,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


class ProcessTreeManager:
    """Tracks child processes + Win32 Job Objects for guaranteed atomic tree teardown."""

    def __init__(self) -> None:
        self._procs: list[tuple[str, subprocess.Popen[Any], int | None]] = []
        self._closed = False
        atexit.register(self.shutdown_all)

    def spawn(
        self,
        name: str,
        cmd: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdout: Any = None,
        stderr: Any = None,
    ) -> subprocess.Popen[Any]:
        job = _create_windows_kill_on_close_job()
        proc = spawn_process_group(cmd, cwd=cwd, env=env, stdout=stdout, stderr=stderr)
        if job and not _assign_to_windows_job(job, proc):
            try:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job))
            except Exception:
                pass
            job = None
        self._procs.append((name, proc, job))
        return proc

    def shutdown_all(self) -> None:
        if self._closed:
            return
        self._closed = True
        for _name, proc, job in reversed(self._procs):
            kill_process_tree(proc, job_handle=job)
        self._procs.clear()


def build_backend_env(
    *,
    base_env: dict[str, str] | None = None,
    llm_mode: str = "auto",
    offline: bool = False,
    db_path: Path | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("AUTH_MODE", "mock")
    effective_llm = "deterministic" if offline else llm_mode
    env["LLM_MODE"] = effective_llm
    env["CONTEXT_LAKE_LLM_MODE"] = effective_llm
    if offline:
        env["CONTEXT_LAKE_OFFLINE"] = "1"
        env.pop("GEMINI_API_KEY", None)
        env.pop("GOOGLE_API_KEY", None)
        env.pop("LANGSMITH_API_KEY", None)
        env.pop("CONTEXT_TRACING_LANGSMITH", None)
        env.pop("CONTEXT_TRACING_INTERNAL_OTLP_ENDPOINT", None)
        env["LANGSMITH_TRACING"] = "false"
    if db_path is not None:
        env["CONTEXT_LAKE_DB"] = str(db_path)
        env["CONTEXT_LAKE_DB_PATH"] = str(db_path)
    pkg_paths = [
        str(REPO_ROOT / "packages" / p)
        for p in ("core", "adapters", "api", "mcp", "sdk", "agents")
    ]
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(pkg_paths + ([existing_pp] if existing_pp else []))
    return env


def start_backend(
    manager: ProcessTreeManager,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    llm_mode: str = "auto",
    offline: bool = False,
    reload: bool = False,
    db_path: Path | None = None,
    stdout: Any = None,
    stderr: Any = None,
) -> subprocess.Popen[Any]:
    py_exe = resolve_venv_python()
    target, is_factory = resolve_uvicorn_target()
    cmd = [
        str(py_exe),
        "-m",
        "uvicorn",
        target,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if is_factory:
        cmd.append("--factory")
    if reload:
        cmd.append("--reload")

    env = build_backend_env(llm_mode=llm_mode, offline=offline, db_path=db_path)
    return manager.spawn("backend", cmd, cwd=REPO_ROOT, env=env, stdout=stdout, stderr=stderr)


def start_frontend(
    manager: ProcessTreeManager,
    *,
    host: str = "127.0.0.1",
    port: int = 5173,
    api_port: int = 8000,
    stdout: Any = None,
    stderr: Any = None,
) -> subprocess.Popen[Any]:
    web_dir = REPO_ROOT / "apps" / "web"
    if not (web_dir / "package.json").is_file():
        raise FileNotFoundError(f"Frontend package.json not found at {web_dir / 'package.json'}")
    env = dict(os.environ)
    env["VITE_API_TARGET"] = f"http://127.0.0.1:{api_port}"
    cmd = [
        resolve_npm_cmd(),
        "run",
        "dev",
        "--",
        "--host",
        host,
        "--port",
        str(port),
        "--strictPort",
    ]
    return manager.spawn("frontend", cmd, cwd=web_dir, env=env, stdout=stdout, stderr=stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Enterprise Context Lake local dev servers.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=5173)
    parser.add_argument("--llm-mode", choices=["auto", "gemini", "deterministic"], default="auto")
    parser.add_argument("--backend-only", action="store_true")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    load_optional_dotenv()
    manager = ProcessTreeManager()

    stop_requested = False

    def _handle_signal(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_signal)

    try:
        if is_port_open(args.host, args.api_port):
            print(f"[dev] ERROR: Port {args.api_port} is already in use.", file=sys.stderr)
            return 1
        if not args.backend_only and is_port_open(args.host, args.web_port):
            print(f"[dev] ERROR: Port {args.web_port} is already in use.", file=sys.stderr)
            return 1

        backend_proc = start_backend(
            manager,
            host=args.host,
            port=args.api_port,
            llm_mode=args.llm_mode,
            reload=args.reload,
        )
        health_url = wait_for_http(
            [
                f"http://{args.host}:{args.api_port}/healthz",
                f"http://{args.host}:{args.api_port}/api/v1/health",
            ],
            timeout_s=45.0,
            proc=backend_proc,
        )

        frontend_proc: subprocess.Popen[Any] | None = None
        if not args.backend_only and (REPO_ROOT / "apps" / "web" / "package.json").is_file():
            frontend_proc = start_frontend(
                manager,
                host=args.host,
                port=args.web_port,
                api_port=args.api_port,
            )
            wait_for_http(
                [f"http://{args.host}:{args.web_port}/"],
                timeout_s=45.0,
                proc=frontend_proc,
            )

        print("\n=== Enterprise Context Lake — Local Dev Ready ===", flush=True)
        print(f"  API / OpenAPI : http://{args.host}:{args.api_port}/docs (health: {health_url})", flush=True)
        print(f"  FastMCP Raw   : http://{args.host}:{args.api_port}/mcp", flush=True)
        print(f"  FastMCP Code  : http://{args.host}:{args.api_port}/mcp/codemode", flush=True)
        print(f"  AX Discovery  : http://{args.host}:{args.api_port}/llms.txt", flush=True)
        if frontend_proc is not None:
            print(f"  Web UI (Vite) : http://{args.host}:{args.web_port}", flush=True)
        print("  Press Ctrl+C to stop and cleanly tear down all child process trees.\n", flush=True)

        while not stop_requested:
            if backend_proc.poll() is not None:
                print(f"[dev] Backend exited with code {backend_proc.returncode}", file=sys.stderr)
                return backend_proc.returncode or 1
            if frontend_proc is not None and frontend_proc.poll() is not None:
                print(f"[dev] Frontend exited with code {frontend_proc.returncode}", file=sys.stderr)
                return frontend_proc.returncode or 1
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[dev] Shutting down process trees...", flush=True)
        manager.shutdown_all()
        print("[dev] Clean shutdown complete.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
