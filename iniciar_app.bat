@echo off
title DJI Neo - Criador de Mapa
echo Inicializando o servidor Flask do painel de controle...
cd /d "C:\Users\mkas2\.gemini\antigravity\scratch\dji_drone_criador_de_mapa"
start "" "http://localhost:5001"
python -u app.py
pause
