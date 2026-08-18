"""
DJI Drone - Criador de Mapas & Orquestrador de Georreferenciamento
Arquitetura Modular LEAN:
- state.py: Estado global thread-safe e logs SSE
- routes_explorer.py: Navegação de pastas e drives
- routes_pipeline.py: Extração de frames, injeção EXIF e KML
- routes_queue.py: Gerenciamento e worker da fila de processamento em lote
- routes_webodm.py: Orquestração Docker do NodeODM e visualizador 3D
- routes_kmz.py: Ajuste visual, fusão contínua, compressão e recorte de KMZ
- kmz_helper.py: Leitor universal e gerador de KMZ em alta performance
"""

import os
from flask import Flask, render_template

from routes_explorer import explorer_bp
from routes_pipeline import pipeline_bp
from routes_queue import queue_bp
from routes_webodm import webodm_bp
from routes_kmz import kmz_bp

app = Flask(__name__, template_folder='templates')

# Registra todos os módulos funcionais
app.register_blueprint(explorer_bp)
app.register_blueprint(pipeline_bp)
app.register_blueprint(queue_bp)
app.register_blueprint(webodm_bp)
app.register_blueprint(kmz_bp)


@app.route('/')
def index():
    return render_template('index.html')


if __name__ == '__main__':
    print("=======================================================")
    print("[DJI Drone - Criador de Mapas] Servidor Iniciado (Porta 5001)")
    print("=======================================================")
    app.run(host='0.0.0.0', port=5001, debug=False)
