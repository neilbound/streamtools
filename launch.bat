@echo off
title streamtools
cd /d "C:\GitHub Repositories\streamtools"
echo Starting streamtools...
".venv312\Scripts\python.exe" -m streamlit run app.py
pause
