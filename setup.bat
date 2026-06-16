@echo off
REM Cria ambiente virtual e instala dependencias. Roda uma unica vez.
cd /d "%~dp0"

echo.
echo === Criando ambiente virtual Python ===
python -m venv .venv
if errorlevel 1 (
    echo ERRO: Python nao encontrado. Instale Python 3.10+ de python.org
    pause
    exit /b 1
)

echo.
echo === Instalando dependencias ===
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo ERRO ao instalar dependencias.
    pause
    exit /b 1
)

echo.
echo === Pronto! ===
echo Agora clique em run.bat para abrir o dashboard.
pause
