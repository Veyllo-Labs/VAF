@echo off
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%"
python -m vaf.main %*
popd
