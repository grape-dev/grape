"""Start Agentic DevOps API server"""
import subprocess, sys, os

# Issue #455：主服务要求 Python >= 3.10
from src.api.version_check import ensure_python_version
ensure_python_version()

cmd = [sys.executable, "-m", "uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "3000", "--reload"]
proc = subprocess.Popen(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))
print(f"Server started (PID={proc.pid}) on http://localhost:3000")
print("Press Ctrl+C to stop")
try:
    proc.wait()
except KeyboardInterrupt:
    proc.terminate()
