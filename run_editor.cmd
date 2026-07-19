@echo off
rem Launch the Supernote Editor (no console window).
pushd "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m supernote_editor %*
popd
