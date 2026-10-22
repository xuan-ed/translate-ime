@echo off
REM ---------------------------------------------------------------
REM translate-ime launcher (Windows)
REM   run.bat               start the IME (detached, no window)
REM   run.bat --foreground  keep the console open (debug / see logs)
REM   run.bat --check       environment self-check
REM   run.bat --demo        preview the candidate window only
REM
REM Why --background is passed below: a console-less process is the whole
REM point of a resident IME. Started in the foreground, closing that black
REM window kills the IME, which looks exactly like "it just stopped
REM working". Commands like --check / --demo / --tray-check return before
REM the background branch, so passing it unconditionally is harmless, and
REM --foreground always wins over it.
REM
REM NOTE: keep this file pure ASCII. cmd.exe reads .bat files using the
REM console OEM codepage (936 on Chinese Windows), so UTF-8 Chinese text
REM here turns into mojibake and - inside a ( ... ) block - breaks the
REM parser outright. Chinese output comes from Python, which writes to
REM the console via WriteConsoleW and is codepage-independent.
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

REM Here --panel is passed, and that is the *only* way the settings window
REM opens on its own (main.py: _want_panel). Why not "open it whenever there
REM is a console": stdin/stdout redirected to NUL still report isatty() ==
REM True on Windows, so a script (or a test) that captures output would get a
REM stray window instead of the console panel it asked for. Explicit beats
REM clever. Pass --no-panel to suppress it (autostart does).
"%PY%" main.py --background --panel %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [run.bat] exit code %RC%
    pause
)

endlocal & exit /b %RC%
