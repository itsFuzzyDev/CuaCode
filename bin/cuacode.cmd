@echo off
setlocal EnableExtensions

for %%I in ("%~dp0..") do set "CUACODE_ROOT=%%~fI"
if not defined CUACODE_WORKER set "CUACODE_WORKER=%CUACODE_ROOT%\main.py"
if not defined CUACODE_PYTHON if exist "%CUACODE_ROOT%\.venv\Scripts\python.exe" set "CUACODE_PYTHON=%CUACODE_ROOT%\.venv\Scripts\python.exe"
if not defined CUACODE_CWD set "CUACODE_CWD=%CD%"

if /I "%~1"=="doctor" goto doctor

rem An upgrade replaces a frontend by renaming the running image aside
rem (quik.exe.old-YYYYMMDD-HHMMSS), because Windows will not let cargo write a
rem file it has mapped. Those copies are dead weight once their process exits --
rem and Windows refuses to delete one while it is still held, so deleting
rem unconditionally is safe: the running one survives, the stale ones go.
rem .running- is the name an earlier build used, so a tree upgraded across that
rem change collects both.
for %%F in ("%CUACODE_ROOT%\rust\target\release\*.exe.old-*" "%CUACODE_ROOT%\rust\target\release\*.exe.running-*") do del /f /q "%%~F" >nul 2>nul

if not exist "%CUACODE_ROOT%\rust\target\release\quik.exe" (
  echo CuaCode's quik frontend is not built.
  echo Run cargo build --release -p cuacode-quik --bin quik from "%CUACODE_ROOT%\rust".
  exit /b 1
)
"%CUACODE_ROOT%\rust\target\release\quik.exe" %*
exit /b %ERRORLEVEL%

:doctor
set "DOCTOR=%CUACODE_ROOT%\rust\target\release\cuacode-doctor.exe"
set "DOCTOR_NEXT=%CUACODE_ROOT%\rust\target\release\cuacode-doctor.next.exe"

if exist "%DOCTOR_NEXT%" (
  move /y "%DOCTOR_NEXT%" "%DOCTOR%" >nul 2>nul
  if errorlevel 1 (
    echo CuaCode doctor could not replace "%DOCTOR%".
    echo Close any other cuacode doctor process and try again.
    exit /b 1
  )
)

if not exist "%DOCTOR%" (
  echo CuaCode's doctor is not built.
  echo Run cargo build --release -p cuacode-rs --bin cuacode-doctor from "%CUACODE_ROOT%\rust".
  exit /b 1
)

pushd "%CUACODE_ROOT%" >nul
"%DOCTOR%" %*
set "STATUS=%ERRORLEVEL%"
popd
exit /b %STATUS%
