#!/bin/bash
cd /home/yangaiwu/grape
export DEVOPS_DB_BACKEND=mysql
export DEVOPS_DB_HOST=127.0.0.1
export DEVOPS_DB_PORT=3306
export DEVOPS_DB_USER=grape
export DEVOPS_DB_PASSWORD=YOUR_SSH_PASSWORD
export DEVOPS_DB_NAME=rds-grape-dev
export OBS_ENABLED=true
export OBS_BUCKET=obs-grape-dev-file
export OBS_ACCESS_KEY=HPUA1RGZONSTYDPQLPZR
export OBS_SECRET_KEY=0Y86GEdGPeOOgV3UhdfzCJMOlFG9cbjVWYu4FF0W
export OBS_ENDPOINT=https://obs.cn-north-4.myhuaweicloud.com
export OBS_PREFIX=grape/prod

PORT=$1
.venv/bin/python -m uvicorn src.api.main:app --host 0.0.0.0 --port $PORT
