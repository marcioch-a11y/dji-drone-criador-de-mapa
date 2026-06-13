import os
import sys
import time
import subprocess
import threading
from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__, template_folder='templates')

# Estado global da aplicação
active_process = None
log_messages = []
process_lock = threading.Lock()

def read_process_output(proc):
    """
    Thread que consome a saída padrão de um processo em tempo real e a direciona para a fila de logs.
    """
    global active_process
    try:
        # Lê linha a linha do buffer unbuffered
        for line in iter(proc.stdout.readline, ''):
            if line:
                log_messages.append(line.strip())
        proc.wait()
    except Exception as e:
        log_messages.append(f"[ERRO] Falha ao ler saída do processo: {e}")
    finally:
        with process_lock:
            active_process = None
        log_messages.append("--- PROCESSO FINALIZADO ---")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/list-directory', methods=['POST'])
def list_directory():
    """
    Lista arquivos e subpastas de um diretório para o explorador de arquivos web.
    """
    import string
    from ctypes import windll

    data = request.json or {}
    path = data.get('path')
    show_files = data.get('show_files', True)
    file_type = data.get('file_type', 'all')

    if not path or not os.path.exists(path):
        path = os.path.expanduser('~')

    path = os.path.abspath(path)

    # Detecta drives disponíveis no Windows
    drives = []
    bitmask = windll.kernel32.GetLogicalDrives()
    for letter in string.ascii_uppercase:
        if bitmask & 1:
            drives.append(f"{letter}:\\")
        bitmask >>= 1

    folders = []
    files = []
    parent_path = os.path.dirname(path) if path != os.path.abspath(os.sep) else None

    try:
        for item in os.listdir(path):
            full_path = os.path.join(path, item)
            # Ignora pastas/arquivos ocultos/sistema
            if item.startswith('.') or item.startswith('$'):
                continue
            if os.path.isdir(full_path):
                folders.append(item)
            elif os.path.isfile(full_path) and show_files:
                ext = item.lower().split('.')[-1] if '.' in item else ''
                if file_type == 'video' and ext not in ['mp4', 'avi', 'mkv', 'mov']:
                    continue
                if file_type == 'srt' and ext != 'srt':
                    continue
                files.append(item)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    folders.sort(key=str.lower)
    files.sort(key=str.lower)

    shortcuts = {
        'Início (Home)': os.path.expanduser('~'),
        'Área de Trabalho (Desktop)': os.path.join(os.path.expanduser('~'), 'Desktop'),
        'Documentos': os.path.join(os.path.expanduser('~'), 'Documents'),
        'Downloads': os.path.join(os.path.expanduser('~'), 'Downloads')
    }
    shortcuts = {k: v for k, v in shortcuts.items() if os.path.exists(v)}

    return jsonify({
        'current_path': path,
        'parent_path': parent_path if parent_path != path else None,
        'drives': drives,
        'folders': folders,
        'files': files,
        'shortcuts': shortcuts
    })




@app.route('/api/status', methods=['GET'])
def status():
    """
    Retorna o status de execução atual.
    """
    global active_process
    with process_lock:
        if active_process is not None:
            return jsonify({'status': 'processing'})
        return jsonify({'status': 'idle'})

@app.route('/api/logs', methods=['GET'])
def logs():
    """
    Endpoint de Server-Sent Events (SSE) para transmissão dos logs em tempo real.
    """
    def event_stream():
        idx = 0
        while True:
            # Se houver novas mensagens de log, envia-as para o cliente
            if idx < len(log_messages):
                yield f"data: {log_messages[idx]}\n\n"
                idx += 1
            else:
                # Se o processo terminou e enviamos tudo, podemos parar?
                # Não, mantém a conexão ativa caso nova tarefa comece. Apenas dorme um pouco.
                time.sleep(0.2)
    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/api/run-pipeline', methods=['POST'])
def run_pipeline():
    """
    Endpoint para iniciar a extração e injeção de coordenadas GPS.
    """
    global active_process, log_messages
    with process_lock:
        if active_process is not None:
            return jsonify({'status': 'error', 'message': 'Já existe uma tarefa em execução no servidor.'}), 400

        data = request.json
        mode = data.get('mode')
        srt = data.get('srt')
        out = data.get('out')
        start = data.get('start', 0.0)
        end = data.get('end')
        force = data.get('force', False)

        if not out:
            return jsonify({'status': 'error', 'message': 'Caminho de destino (OUT) ausente.'}), 400
        if mode == 'video' and not srt:
            return jsonify({'status': 'error', 'message': 'Caminho da telemetria (SRT) é obrigatório no modo vídeo.'}), 400

        # Constrói comando CLI para rodar o script main.py
        cmd = [sys.executable, "-u", "main.py", "--out", out, "--start", str(start)]

        if srt:
            cmd += ["--srt", srt]
        if end is not None:
            cmd += ["--end", str(end)]
        if force:
            cmd.append("--force")


        if mode == 'video':
            video = data.get('video')
            interval = data.get('interval', 1.5)
            if not video:
                return jsonify({'status': 'error', 'message': 'Caminho do vídeo é obrigatório no modo vídeo.'}), 400
            cmd += ["--video", video, "--interval", str(interval)]
        else:
            photos = data.get('photos')
            photo_interval = data.get('photo_interval', 2.0)
            filt = data.get('filter', '*.jpg')
            match_datetime = data.get('match_datetime', False)
            if not photos:
                return jsonify({'status': 'error', 'message': 'Diretório de fotos é obrigatório no modo fotos.'}), 400
            cmd += ["--photos", photos, "--photo-interval", str(photo_interval), "--filter", filt]
            if match_datetime:
                cmd.append("--match-datetime")

        # Limpa os logs e inicia
        log_messages = ["--- INICIANDO PIPELINE DE GEORREFERENCIAMENTO ---"]
        print(f"[App] Executando comando: {' '.join(cmd)}")

        try:
            active_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            # Inicia thread para monitoramento síncrono
            thread = threading.Thread(target=read_process_output, args=(active_process,))
            thread.daemon = True
            thread.start()
            return jsonify({'status': 'started'})
        except Exception as e:
            active_process = None
            return jsonify({'status': 'error', 'message': f'Erro ao iniciar subprocesso: {str(e)}'}), 500

@app.route('/api/run-webodm', methods=['POST'])
def run_webodm():
    """
    Endpoint para iniciar a costura e processamento no WebODM.
    Orquestra automaticamente a inicialização e o desligamento do container docker NodeODM.
    """
    global active_process, log_messages
    with process_lock:
        if active_process is not None:
            return jsonify({'status': 'error', 'message': 'Já existe uma tarefa em execução no servidor.'}), 400

        data = request.json
        photos = data.get('photos')
        out = data.get('out')
        filt = data.get('filter', '*.jpg')
        quality = data.get('quality', 'medium')
        resolution = data.get('resolution', 4.0)

        if not photos or not out:
            return jsonify({'status': 'error', 'message': 'Parâmetros obrigatórios PHOTOS e OUT ausentes.'}), 400

        # Orquestrador assíncrono em thread
        def run_webodm_orchestrated():
            global active_process
            try:
                # 1. Tenta subir o docker container do NodeODM
                log_messages.append("[Orquestrador] Inicializando container do NodeODM (porta 3000)...")
                docker_start = subprocess.run(
                    ["docker", "run", "-d", "--name", "temp-nodeodm", "-p", "3000:3000", "webodm/nodeodm:stable"],
                    capture_output=True, text=True
                )
                
                # Se falhar porque o container já existe, remove o antigo e sobe novamente
                if docker_start.returncode != 0:
                    log_messages.append("[Orquestrador] Container antigo detectado. Reiniciando temp-nodeodm...")
                    subprocess.run(["docker", "rm", "-f", "temp-nodeodm"], capture_output=True)
                    docker_start = subprocess.run(
                        ["docker", "run", "-d", "--name", "temp-nodeodm", "-p", "3000:3000", "webodm/nodeodm:stable"],
                        capture_output=True, text=True
                    )
                
                if docker_start.returncode == 0:
                    log_messages.append("[Orquestrador] Container iniciado! Aguardando 4s para estabilização da API...")
                    time.sleep(4)
                else:
                    log_messages.append(f"[Orquestrador ERRO] Falha crítica ao iniciar Docker: {docker_start.stderr}")
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
                
                log_messages.append(f"[Orquestrador] Iniciando processo no NodeODM...")
                print(f"[App] Executando comando: {' '.join(cmd)}")
                
                with process_lock:
                    active_process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1
                    )
                
                # Consome os logs do script pyodm
                for line in iter(active_process.stdout.readline, ''):
                    if line:
                        log_messages.append(line.strip())
                active_process.wait()

            except Exception as e:
                log_messages.append(f"[Orquestrador ERRO] Falha na execução da tarefa: {e}")
            finally:
                # 3. Libera recursos desligando o container
                log_messages.append("[Orquestrador] Desativando e removendo container do NodeODM para liberar CPU/RAM...")
                subprocess.run(["docker", "rm", "-f", "temp-nodeodm"], capture_output=True)
                log_messages.append("[Orquestrador] Recursos liberados com sucesso!")
                with process_lock:
                    active_process = None
                log_messages.append("--- PROCESSO FINALIZADO ---")

        log_messages = ["--- INICIANDO PROCESSAMENTO NO WEBODM ---"]
        try:
            # Marcamos active_process com uma string para o painel reconhecer como ativo
            active_process = "webodm_flow"
            thread = threading.Thread(target=run_webodm_orchestrated)
            thread.daemon = True
            thread.start()
            return jsonify({'status': 'started'})
        except Exception as e:
            active_process = None
            return jsonify({'status': 'error', 'message': f'Erro ao iniciar tarefa do WebODM: {str(e)}'}), 500

if __name__ == '__main__':
    # Roda o servidor Flask localmente na porta 5000
    app.run(host='0.0.0.0', port=5000, debug=False)
