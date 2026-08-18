import os
import sys
import time
import subprocess
import threading
from flask import Blueprint, request, jsonify, make_response, render_template, send_file, send_from_directory
import state

webodm_bp = Blueprint('webodm_bp', __name__)

@webodm_bp.route('/api/run-webodm', methods=['POST'])
def run_webodm():
    """
    Endpoint para iniciar a costura e processamento no WebODM.
    Orquestra automaticamente a inicialização e o desligamento do container docker NodeODM.
    """
    with state.process_lock:
        if state.active_process is not None:
            return jsonify({'status': 'error', 'message': 'Já existe uma tarefa em execução no servidor.'}), 400

        data = request.json or {}
        photos = data.get('photos')
        out = data.get('out')
        filt = data.get('filter', '*.jpg')
        quality = data.get('quality', 'medium')
        resolution = data.get('resolution', 4.0)
        kmz_name = data.get('kmz_name', '')

        if not photos or not out:
            return jsonify({'status': 'error', 'message': 'Parâmetros obrigatórios PHOTOS e OUT ausentes.'}), 400

        def run_webodm_orchestrated():
            try:
                # 1. Tenta subir o docker container do NodeODM
                state.add_log("[Orquestrador] Inicializando container do NodeODM (porta 3000)...")
                docker_start = subprocess.run(
                    ["docker", "run", "-d", "--name", "temp-nodeodm", "-p", "3000:3000", "webodm/nodeodm:stable"],
                    capture_output=True, text=True
                )
                
                if docker_start.returncode != 0:
                    state.add_log("[Orquestrador] Container antigo detectado. Reiniciando temp-nodeodm...")
                    subprocess.run(["docker", "rm", "-f", "temp-nodeodm"], capture_output=True)
                    docker_start = subprocess.run(
                        ["docker", "run", "-d", "--name", "temp-nodeodm", "-p", "3000:3000", "webodm/nodeodm:stable"],
                        capture_output=True, text=True
                    )
                
                if docker_start.returncode == 0:
                    state.add_log("[Orquestrador] Container iniciado! Aguardando 4s para estabilização da API...")
                    time.sleep(4)
                else:
                    state.add_log(f"[Orquestrador ERRO] Falha crítica ao iniciar Docker: {docker_start.stderr}")
                    return

                # 2. Executa o script processar_webodm.py
                cmd = [
                    sys.executable, "-u", "processar_webodm.py",
                    "--photos", photos,
                    "--out", out,
                    "--filter", filt,
                    "--quality", quality,
                    "--resolution", str(resolution)
                ]
                if kmz_name:
                    cmd += ["--kmz-name", kmz_name]
                
                state.add_log("[Orquestrador] Iniciando processo no NodeODM...")
                print(f"[App] Executando comando: {' '.join(cmd)}")
                
                with state.process_lock:
                    state.active_process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1
                    )
                
                for line in iter(state.active_process.stdout.readline, ''):
                    if line:
                        state.add_log(line.strip())
                state.active_process.wait()

            except Exception as e:
                state.add_log(f"[Orquestrador ERRO] Falha na execução da tarefa: {e}")
            finally:
                state.add_log("[Orquestrador] Desativando e removendo container do NodeODM para liberar CPU/RAM...")
                subprocess.run(["docker", "rm", "-f", "temp-nodeodm"], capture_output=True)
                state.add_log("[Orquestrador] Recursos liberados com sucesso!")
                with state.process_lock:
                    state.active_process = None
                state.add_log("--- PROCESSO FINALIZADO ---")

        state.clear_logs("--- INICIANDO PROCESSAMENTO NO WEBODM ---")
        try:
            state.active_process = "webodm_flow"
            thread = threading.Thread(target=run_webodm_orchestrated)
            thread.daemon = True
            thread.start()
            return jsonify({'status': 'started'})
        except Exception as e:
            state.active_process = None
            return jsonify({'status': 'error', 'message': f'Erro ao iniciar tarefa do WebODM: {str(e)}'}), 500


@webodm_bp.route('/viewer-3d')
def viewer_3d():
    """
    Visualizador 3D do modelo texturizado gerado pelo WebODM.
    """
    model_path = request.args.get('path', r"C:\Users\mkas2\Desktop\talhao\teste\Resultado_webodm\odm_texturing\odm_textured_model_geo.glb")
    response = make_response(render_template('viewer_3d.html', model_path=model_path))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@webodm_bp.route('/api/3d-model')
def get_3d_model():
    """
    Serve o arquivo GLB do modelo 3D.
    """
    model_path = request.args.get('path', r"C:\Users\mkas2\Desktop\talhao\teste\Resultado_webodm\odm_texturing\odm_textured_model_geo.glb")
    if os.path.exists(model_path):
        return send_file(model_path, mimetype='model/gltf-binary', conditional=True)
    else:
        return jsonify({'error': 'Modelo 3D não encontrado'}), 404


@webodm_bp.route('/api/odm-static/<path:filename>')
def serve_odm_static(filename):
    """
    Serve arquivos estáticos da pasta do WebODM (OBJ, MTL, Imagens de textura).
    """
    directory = r"C:\Users\mkas2\Desktop\talhao\teste\Resultado_webodm\odm_texturing"
    return send_from_directory(directory, filename)
