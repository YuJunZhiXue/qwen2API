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
    npm_build_cmd = "npm run build" if is_windows else ["npm", "run", "build"]
    
    # 检查前端 node_modules 是否存在，如果不存在或为了安全起见，执行 npm install 和 build
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
        
        print("  -> 正在强制清空旧版静态缓存并执行 npm run build 构建新资源...")
        # 跨平台强制清空 dist 目录，防止 Vite 复用旧的 index.html
        import shutil
        dist_dir = FRONTEND_DIR / "dist"
        if dist_dir.exists():
            shutil.rmtree(dist_dir)
            
        subprocess.check_call(
            npm_build_cmd,
            cwd=FRONTEND_DIR,
            shell=is_windows,
            stdout=None, 
            stderr=subprocess.STDOUT
        )
        print("✓ [预检通过] 前端依赖及构建已就绪。")
    except subprocess.CalledProcessError as e:
        print(f"❌ [预检失败] 前端构建失败: {e}")
        sys.exit(1)
    
def start_backend() -> subprocess.Popen:
    print("⚡ 正在唤醒底层铁壁 (Backend)...")
    log_file = open(LOGS_DIR / "backend.log", "w", encoding="utf-8")
    
    # 根据系统判断 python 执行文件
    python_exec = sys.executable
    
    # 注入 PYTHONPATH，让 backend 内的绝对导入生效
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKSPACE_DIR)
    
    proc = subprocess.Popen(
        [python_exec, "backend/main.py"],
        cwd=WORKSPACE_DIR,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT
    )
    print(f"✓ Backend 已点火 (PID: {proc.pid}) -> 日志: logs/backend.log")
    return proc

def main():
    ensure_dirs()
    check_and_install_dependencies()
    
    backend_proc = start_backend()
    
    print("\n==========================================")
    print("帝国已上线。")
    print("▶ 统一中枢入口: http://localhost:8080")
    print("==========================================")
    print("按 Ctrl+C 掐断进程并关闭系统。")
    
    def signal_handler(sig, frame):
        print("\n\n⚠ 收到关闭指令，正在掐断进程...")
        backend_proc.terminate()
        backend_proc.wait()
        print("✓ 进程已被摧毁，帝国下线。")
        sys.exit(0)
        
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 保持主进程存活，同时监控后端状态
    try:
        while True:
            if backend_proc.poll() is not None:
                print(f"❌ Backend 异常退出 (Exit Code: {backend_proc.returncode})")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if backend_proc.poll() is None: backend_proc.terminate()

if __name__ == "__main__":
    main()
