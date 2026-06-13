@echo off
title DJI Neo - Criador de Mapa
echo Inicializando o servidor Flask do painel de controle...
cd /d "%~dp0"
start "" "http://localhost:5000"
python -u app.py
pause
