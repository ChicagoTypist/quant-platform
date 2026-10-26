@echo off
chcp 936 >nul 2>nul
title 环境自检
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo.

set "PYEXE="
if exist "%~dp0python\python.exe" set "PYEXE=%~dp0python\python.exe"
if not defined PYEXE if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PYEXE=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PYEXE (
  where py.exe >nul 2>nul && set "PYEXE=py -3"
)
if not defined PYEXE (
  where python.exe >nul 2>nul && set "PYEXE=python"
)

if not defined PYEXE (
  echo   [X] 没有找到 Python，无法自检。
  echo   请先安装 Python 3.9 以上版本，安装时勾选 Add Python to PATH。
  echo.
  pause
  exit /b 1
)

!PYEXE! -m app.doctor

echo.
pause >nul
