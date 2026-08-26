@echo off
venv\Scripts\python.exe fetch_solio.py
venv\Scripts\python.exe draft_lineup.py %*
