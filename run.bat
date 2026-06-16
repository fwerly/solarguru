@echo off
REM Inicia o dashboard. Abre o navegador automaticamente.
cd /d "%~dp0"

if not exist .venv (
    echo Ambiente virtual nao encontrado. Rode setup.bat primeiro.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
streamlit run app.py
