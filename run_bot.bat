@echo off
cd /d "D:\Quote Bot BE"
call venv\Scripts\activate
title Jai Electricals Quote Bot
python daemon.py
pause