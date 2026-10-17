@echo off
cd /d D:\code\Hello-agent
D:\conda-envs\helloagent\python.exe run_daily_brief.py >> outputs\daily_briefs\scheduled_task.log 2>&1
