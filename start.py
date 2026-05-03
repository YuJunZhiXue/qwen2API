#!/usr/bin/env python3
"""
qwen2API Enterprise Gateway Startup Script

Frontend: Vite dev server  http://localhost:5174  (Hot reload)
Backend: uvicorn          http://localhost:7860  (API gateway)
"""
import os
import sys
import subprocess
import time
import signal
from pathlib import Path

WORKSPACE_DIR = Path(__file__).parent.absolute()
BACKEND_DIR = WORKSPACE_DIR / "backend"
FRONTEND_DIR = WORKSPACE_DIR / "frontend"
LOGS_DIR = WORKSPACE_DIR / "logs"
DATA_DIR = WORKSPACE_DIR / "data"


def ensure_dirs():
    LOGS_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)


def check_python():
    if sys.version_info < (3, 10):
        print("❌ Python 3.10+ required, current version:", sys.version)
        sys.exit(1)


def install_backend_deps():
    print("⚡ [1/4] Installing backend dependencies...")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKSPACE_DIR)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"],
            cwd=BACKEND_DIR,
            env=env,
        )
        print("✓ Backend dependencies ready")
    except Exception as e:
        print(f"⚠ Backend dependency installation error: {e}")


def fetch_browser():
    print("⚡ [2/4] Checking Camoufox browser engine for registration...")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKSPACE_DIR)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "camoufox", "path"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            print("✓ Browser engine already exists, skipping download")
            return
    except Exception:
        pass
    print("  -> Downloading Camoufox engine (used only for registration/activation, please wait)...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "camoufox", "fetch"],
            cwd=WORKSPACE_DIR,
            env=env,
        )
        print("✓ Browser engine download complete")
    except Exception as e:
        print(f"⚠ Browser engine download error: {e}")


def start_frontend() -> subprocess.Popen:
    print("⚡ [3/4] Starting frontend development server...")
    is_windows = os.name == "nt"

    if not (FRONTEND_DIR / "node_modules").exists():
        print("  -> Running npm install...")
        try:
            subprocess.check_call(
                "npm install" if is_windows else ["npm", "install"],
                cwd=FRONTEND_DIR,
                shell=is_windows,
            )
        except subprocess.CalledProcessError as e:
            print(f"❌ npm install failed: {e}")
            sys.exit(1)

    proc = subprocess.Popen(
        "npm run dev" if is_windows else ["npm", "run", "dev"],
        cwd=FRONTEND_DIR,
        shell=is_windows,
    )
    print(f"✓ Frontend started (PID: {proc.pid})  →  http://127.0.0.1:5174")
    return proc


def kill_port(port: int):
    """Kill any process occupying the given port."""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                        print(f"  -> Terminated process occupying port {port} (PID: {pid})")
                        time.sleep(1)
                        return
        else:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True, timeout=5
            )
            pid = result.stdout.strip()
            if pid:
                subprocess.run(["kill", "-9", pid], capture_output=True)
                print(f"  -> Terminated process occupying port {port} (PID: {pid})")
                time.sleep(1)
    except Exception:
        pass


def start_backend() -> subprocess.Popen:
    print("⚡ [4/4] 启动后端服务...")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKSPACE_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    port = env.get("PORT", "7860")
    workers = env.get("WORKERS", "1")
    kill_port(int(port))

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "backend.main:app",
            "--host", "0.0.0.0",
            "--port", port,
            "--workers", workers,
        ],
        cwd=WORKSPACE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    print(f"✓ Backend process started (PID: {proc.pid}), waiting for service initialization...")

    import threading
    ready_event = threading.Event()

    def read_output():
        for line in iter(proc.stdout.readline, b""):
            try:
                decoded = line.decode("utf-8", errors="replace")
            except Exception:
                decoded = str(line)
            print(decoded, end="")
            if "Application startup complete" in decoded or "Service fully ready" in decoded:
                ready_event.set()

    threading.Thread(target=read_output, daemon=True).start()

    started = ready_event.wait(timeout=300)
    if not started:
        print("⚠ Backend initialization timeout, service may not be fully ready")
    else:
        print("✓ Service fully ready")

    return proc


def main():
    ensure_dirs()
    check_python()
    install_backend_deps()
    fetch_browser()
    backend_proc = start_backend()
    frontend_proc = start_frontend()

    port = os.environ.get("PORT", "7860")
    print()
    print("=" * 50)
    print("  qwen2API is now online")
    print(f"  Frontend WebUI:   http://127.0.0.1:5174")
    print(f"  Backend API:     http://127.0.0.1:{port}")
    print("=" * 50)
    print("  Press Ctrl+C to stop all services")
    print()

    def signal_handler(sig, frame):
        print("\nShutting down services...")
        for p in (backend_proc, frontend_proc):
            try:
                p.terminate()
            except Exception:
                pass
        backend_proc.wait()
        print("Services stopped")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        while True:
            if backend_proc.poll() is not None:
                print(f"❌ Backend process exited unexpectedly (exit code: {backend_proc.returncode})")
                break
            if frontend_proc.poll() is not None:
                print(f"❌ Frontend process exited unexpectedly (exit code: {frontend_proc.returncode})")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in (backend_proc, frontend_proc):
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    main()
