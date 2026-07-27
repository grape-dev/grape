@echo off
cd /d "%~dp0"
echo Starting Agentic DevOps API on http://localhost:3000
python -m uvicorn src.api.main:app --host 0.0.0.0 --port 3000 --reload
pause
