@echo off
rem VAF command shim. Forwards to run_vaf.bat (which activates the venv and runs
rem `python -m vaf.main <args>`), so `vaf <command>` works from any terminal once the
rem install directory is on PATH (install.ps1 adds it). Examples:
rem   vaf            -> starts the desktop app (tray + web UI)
rem   vaf update     -> checks for and applies a new VAF release
call "%~dp0run_vaf.bat" %*
