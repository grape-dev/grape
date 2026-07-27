import subprocess, sys
r = subprocess.run(["python3", "-c", "import fastapi; print(fastapi.__version__)"], capture_output=True, text=True, timeout=15)
if r.returncode == 0:
    print("fastapi OK:", r.stdout.strip())
else:
    print("fastapi not installed, installing...")
    ir = subprocess.run(["pip", "install", "fastapi", "uvicorn", "--break-system-packages"], capture_output=True, text=True, timeout=120)
    print(ir.stdout[-200:] if ir.stdout else "no output")
    print(ir.stderr[:200] if ir.stderr else "no stderr")
