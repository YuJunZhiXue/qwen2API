#!/usr/bin/env python3
"""
qwen2API Enterprise Gateway 启动脚本

前端: Vite dev server  http://localhost:5174  (热更新)
后端: uvicorn          http://localhost:7860  (API 网关)
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
        print("❌ 需要 Python 3.10+，当前版本:", sys.version)
        sys.exit(1)


def install_backend_deps():
    print("⚡ [1/4] 安装后端依赖...")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKSPACE_DIR)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"],
            cwd=BACKEND_DIR,
            env=env,
        )
        print("✓ 后端依赖已就绪")
    except Exception as e:
        print(f"⚠ 后端依赖安装异常: {e}")


def fetch_browser():
    print("⚡ [2/4] 检查 Camoufox 浏览器内核...")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKSPACE_DIR)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "camoufox", "path"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            print("✓ 浏览器内核已存在，跳过下载")
            return
    except Exception:
        pass
    print("  -> 正在下载 Camoufox 内核（首次运行，请耐心等待）...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "camoufox", "fetch"],
            cwd=WORKSPACE_DIR,
            env=env,
        )
        print("✓ 浏览器内核下载完成")
    except Exception as e:
        print(f"⚠ 浏览器内核下载异常: {e}")


def start_frontend() -> subprocess.Popen:
    print("⚡ [3/4] 启动前端开发服务器...")
    is_windows = os.name == "nt"

    if not (FRONTEND_DIR / "node_modules").exists():
        print("  -> 正在执行 npm install...")
        try:
            subprocess.check_call(
                "npm install" if is_windows else ["npm", "install"],
                cwd=FRONTEND_DIR,
                shell=is_windows,
            )
        except subprocess.CalledProcessError as e:
            print(f"❌ npm install 失败: {e}")
            sys.exit(1)

    proc = subprocess.Popen(
        "npm run dev" if is_windows else ["npm", "run", "dev"],
        cwd=FRONTEND_DIR,
        shell=is_windows,
    )
    print(f"✓ 前端已启动 (PID: {proc.pid})  →  http://127.0.0.1:5174")
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
                        print(f"  -> 已终止占用 {port} 端口的旧进程 (PID: {pid})")
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
                print(f"  -> 已终止占用 {port} 端口的旧进程 (PID: {pid})")
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
    kill_port(int(port))

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "backend.main:app",
            "--host", "0.0.0.0",
            "--port", port,
            "--workers", "1",
        ],
        cwd=WORKSPACE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    print(f"✓ 后端进程已启动 (PID: {proc.pid})，正在初始化浏览器引擎...")

    import threading
    ready_event = threading.Event()

    def read_output():
        for line in iter(proc.stdout.readline, b""):
            try:
                decoded = line.decode("utf-8", errors="replace")
            except Exception:
                decoded = str(line)
            print(decoded, end="")
            if "Browser engine started" in decoded or "Application startup complete" in decoded:
                ready_event.set()

    threading.Thread(target=read_output, daemon=True).start()

    started = ready_event.wait(timeout=300)
    if not started:
        print("⚠ 后端初始化超时，服务可能未完全就绪")
    else:
        print("✓ 服务已完全就绪")

    return proc


def main():
    ensure_dirs()
    check_python()
    install_backend_deps()
    fetch_browser()
    backend_proc = start_backend()   # 先等浏览器引擎完全就绪
    frontend_proc = start_frontend() # 引擎好了再启前端

    port = os.environ.get("PORT", "7860")
    print()
    print("=" * 50)
    print("  qwen2API 已上线")
    print(f"  前端 WebUI:   http://127.0.0.1:5174")
    print(f"  后端 API:     http://127.0.0.1:{port}")
    print("=" * 50)
    print("  按 Ctrl+C 停止所有服务")
    print()

    def signal_handler(sig, frame):
        print("\n正在关闭服务...")
        for p in (backend_proc, frontend_proc):
            try:
                p.terminate()
            except Exception:
                pass
        backend_proc.wait()
        print("服务已停止")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        while True:
            if backend_proc.poll() is not None:
                print(f"❌ 后端进程异常退出 (Exit Code: {backend_proc.returncode})")
                break
            if frontend_proc.poll() is not None:
                print(f"❌ 前端进程异常退出 (Exit Code: {frontend_proc.returncode})")
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
