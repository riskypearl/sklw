@echo off
venv\Scripts\python.exe fetch_solio.py
venv\Scripts\python.exe sklw_lineup.py %*
