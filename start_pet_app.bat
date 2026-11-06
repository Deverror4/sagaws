@echo off
title Pet Farm Helper
cd /d "%~dp0"
rem Starting the app makes any older pet sync / app close itself (see pet_sync.pid).
rem The window only stays open if the app stops with an error, so you can read it.
python pet_app.py || pause
