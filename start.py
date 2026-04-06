#!/usr/bin/env python3
import os
import sys
import subprocess
import time
import signal
from pathlib import Path

# ==========================================
# qwen2API Enterprise Gateway - Python 跨平台点火脚本
# ==========================================

WORKSPACE_DIR = Path(__file__).parent.absolute()
BACKEND_DIR = WORKSPACE_DIR / "backend"
FRONTEND_DIR = WORKSPACE_DIR / "frontend"
LOGS_DIR = WORKSPACE_DIR / "logs"

def ensure_dirs():
    LOGS_DIR.mkdir(exist_ok=True)
    (WORKSPACE_DIR / "data").mkdir(exist_ok=True)

def check_and_install_dependencies():
    print("⚡ [系统预检] 正在扫描底层铁壁的 Python 环境...")
    python_exec = sys.executable
    
    # 注入环境变量，防止 Windows 下 pip 安装的包找不到
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKSPACE_DIR)
    
    # 安装后端依赖
    try:
        subprocess.check_call(
            [python_exec, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"],
            cwd=BACKEND_DIR,
            env=env,
            stdout=None, # 将 pip 安装日志直接输出，暴露错误
            stderr=subprocess.STDOUT
        )
    except Exception as e:
        print(f"⚠ [预检警告] 后端依赖安装异常: {e}")
        
    print("⚡ [系统预检] 正在下载并配置浏览器内核 (Camoufox)...")
    try:
        # 在 Windows 上，有时候 pip 安装的全局包不能通过 python -m 直接调用（特别是多版本或权限问题）
        # 改用更通用的 shell=True 执行，让系统自动在 Scripts 目录里找 camoufox
        subprocess.check_call(
            "camoufox fetch" if os.name == "nt" else [python_exec, "-m", "camoufox", "fetch"],
            cwd=WORKSPACE_DIR,
            shell=(os.name == "nt"),
            env=env,
            stdout=None, # 将输出打印到终端，暴露详细报错
            stderr=subprocess.STDOUT
        )
    except Exception as e:
        print(f"⚠ [预检警告] 浏览器内核配置异常: {e}")

    print("⚡ [系统预检] 正在扫描前端王座的 Node 环境...")
    is_windows = (os.name == "nt")
    npm_install_cmd = "npm install" if is_windows else ["npm", "install"]
    
    # 检查前端 node_modules 是否存在，如果不存在或为了安全起见，执行 npm install
    try:
        # 为了给用户一个清晰的进度，不吞噬这里的输出
        print("  -> 正在执行 npm install (可能需要一点时间，请耐心等待)...")
        subprocess.check_call(
            npm_install_cmd,
            cwd=FRONTEND_DIR,
            shell=is_windows,
            stdout=None, # 将 npm install 的日志也直接输出到终端，让你能看到为什么 npm 失败
            stderr=subprocess.STDOUT
        )
        print("✓ [预检通过] 前端依赖已就绪。")
    except subprocess.CalledProcessError as e:
        print(f"❌ [预检失败] 前端构建失败: {e}")
        sys.exit(1)
    
def start_backend() -> subprocess.Popen:
    print("⚡ 正在唤醒底层铁壁 (Backend)...")
    
    # 根据系统判断 python 执行文件
    python_exec = sys.executable
    
    # 注入 PYTHONPATH，让 backend 内的绝对导入生效
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKSPACE_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    
    # 为了能读取输出并判断启动完毕，我们接管 stdout，并使用线程边读边打印
    proc = subprocess.Popen(
        [python_exec, "backend/main.py"],
        cwd=WORKSPACE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0, # 在二进制模式下，使用无缓冲 (0) 或默认缓冲 (-1)，不能使用行缓冲 (1)
    )
    print(f"✓ Backend 进程已点火 (PID: {proc.pid})，正在初始化千问浏览器内核，请耐心等待...")
    
    import threading
    
    # 使用事件来通知主线程初始化完成
    ready_event = threading.Event()
    
    def read_output():
        for line in iter(proc.stdout.readline, b''):
            try:
                decoded_line = line.decode('utf-8', errors='replace')
            except Exception:
                decoded_line = str(line)
            print(decoded_line, end="")
            if "Browser engine started" in decoded_line or "Application startup complete" in decoded_line:
                ready_event.set()
                
    t = threading.Thread(target=read_output, daemon=True)
    t.start()
    
    # 阻塞等待，最多等 5 分钟
    started = ready_event.wait(timeout=300)
    if not started:
        print("❌ [严重警告] 后端内核初始化超时或失败，系统可能无法正常工作！")
    else:
        print("✓ 底层铁壁已完全就绪。")
        
    return proc

def start_frontend() -> subprocess.Popen:
    print("⚡ 正在唤醒前端面板 (Admin Dashboard)...")
    log_file = open(LOGS_DIR / "frontend.log", "w", encoding="utf-8")
    
    is_windows = (os.name == "nt")
    npm_cmd = "npm run dev" if is_windows else ["npm", "run", "dev"]
    
    proc = subprocess.Popen(
        npm_cmd,
        cwd=FRONTEND_DIR,
        shell=is_windows, 
        stdout=None, # 将输出直接抛到终端
        stderr=None
    )
    print(f"✓ Frontend 已点火 (PID: {proc.pid}) -> 终端直出报错")
    return proc

def main():
    ensure_dirs()
    check_and_install_dependencies()
    
    backend_proc = start_backend()
    
    # 因为 start_backend 已经是强阻塞（等待 ready_event），到这里说明后端必定已经 ready
    frontend_proc = start_frontend()
    
    print("\n==========================================")
    print("系统已上线。")
    print("▶ 控制台入口: http://localhost:5173")
    print("▶ API 接口:   http://localhost:8080")
    print("==========================================")
    print("按 Ctrl+C 掐断进程并关闭系统。")
    
    def signal_handler(sig, frame):
        print("\n\n⚠ 收到关闭指令，正在掐断进程...")
        backend_proc.terminate()
        frontend_proc.terminate()
        backend_proc.wait()
        frontend_proc.wait()
        print("✓ 进程已被摧毁，系统下线。")
        sys.exit(0)
        
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 保持主进程存活，同时监控状态
    try:
        while True:
            if backend_proc.poll() is not None:
                print(f"❌ Backend 异常退出 (Exit Code: {backend_proc.returncode})")
                break
            if frontend_proc.poll() is not None:
                print(f"❌ Frontend 异常退出 (Exit Code: {frontend_proc.returncode})")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if backend_proc.poll() is None: backend_proc.terminate()
        if frontend_proc.poll() is None: frontend_proc.terminate()

if __name__ == "__main__":
    main()