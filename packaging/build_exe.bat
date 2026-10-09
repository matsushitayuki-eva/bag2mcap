@echo off
rem bag2mcap.exe をビルドする（Windows / Python 3.12 推奨）
cd /d %~dp0\..
python -m venv .venv || exit /b 1
call .venv\Scripts\activate.bat
python -m pip install -U pip
pip install -r requirements-dev.txt || exit /b 1
pip install -e . || exit /b 1
python -m pytest -q || exit /b 1
pyinstaller --noconfirm --clean --onedir --windowed --name bag2mcap ^
  --collect-all tkinterdnd2 --paths src packaging\run_gui.py || exit /b 1
echo.
echo 完了: dist\bag2mcap\bag2mcap.exe
