@echo off
title Pet Sync - keep this open while farming
cd /d "%~dp0"
rem Starting pet sync makes any older one close itself (see pet_sync.pid).
rem The window only stays open if pet sync stops with an error, so you can read it.
python pet_sync.py || pause
