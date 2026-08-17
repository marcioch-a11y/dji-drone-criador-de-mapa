import os
import sys
import time
import subprocess
import threading
from flask import Flask, render_template, request, jsonify, Response, send_file, send_from_directory, make_response

app = Flask(__name__, template_folder='templates')

# Estado global da aplicação
active_process = None
log_messages = []
process_lock = threading.Lock()

# Sistema de Fila de Projetos (Batch / Lista de Espera Noturna)
project_queue = []
queue_lock = threading.Lock()
current_job = None
queue_worker_thread = None

def run_project_job(job):
    """
    Executa um projeto completo da fila:
    Passo 1: Georreferenciamento (main.py)
    Passo 2 (opcional): Processamento WebODM / Orquestrador Docker (processar_webodm.py)
    """
    global active_process, log_messages, current_job
    job_id = job.get('id')
    job_name = job.get('name', 'Projeto')
    
    with queue_lock:
        job['status'] = 'running'
        current_job = job

    log_messages.append(f"\n=======================================================")
    log_messages.append(f"[Fila de Projetos] INICIANDO: {job_name}")
    log_messages.append(f"=======================================================")

    mode = job.get('mode', 'video')
    srt = job.get('srt')
    out = job.get('out')
    start = job.get('start', 0.0)
    end = job.get('end')
    force = job.get('force', False)
    auto_map = job.get('auto_map', True)
    mesh_3d = job.get('mesh_3d', False)
    quality = job.get('quality', 'medium')
    resolution = job.get('resolution', 4.0)
    kmz_name = job.get('kmz_name', '')

    # --- PASSO 1: Georreferenciamento ---
    cmd_geo = [sys.executable, "-u", "main.py", "--out", out, "--start", str(start)]
    if srt:
        cmd_geo += ["--srt", srt]
    if end is not None:
        cmd_geo += ["--end", str(end)]
    if force:
        cmd_geo.append("--force")

    if mode == 'video':
        video = job.get('video')
        interval = job.get('interval', 1.5)
        cmd_geo += ["--video", video, "--interval", str(interval)]
    else:
        photos = job.get('photos')
        photo_interval = job.get('photo_interval', 2.0)
        filt = job.get('filter', '*.jpg')
        cmd_geo += ["--photos", photos, "--photo-interval", str(photo_interval), "--filter", filt]
        if job.get('match_datetime', False):
            cmd_geo.append("--match-datetime")

    log_messages.append(f"[Fila: Passo 1] Extraindo e georreferenciando fotos...")
    try:
        with process_lock:
            active_process = subprocess.Popen(
                cmd_geo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
        
        for line in iter(active_process.stdout.readline, ''):
            if line:
                log_messages.append(line.strip())
        active_process.wait()
        geo_ret = active_process.returncode
    except Exception as e:
        log_messages.append(f"[ERRO no Georreferenciamento]: {e}")
        geo_ret = -1
    finally:
        with process_lock:
            active_process = None

    if geo_ret != 0:
        log_messages.append(f"[Fila AVISO] Georreferenciamento finalizou com código {geo_ret}. Abortando mapa para este projeto.")
        with queue_lock:
            job['status'] = 'failed'
            current_job = None
        return

    log_messages.append(f"[Fila: Passo 1 Concluído] Fotos salvas com sucesso em: {out}")

    # Se auto_map não estiver ativado, conclui aqui
    if not auto_map:
        log_messages.append(f"[Fila] Projeto {job_name} concluído (Criação de Mapa WebODM estava desativada).")
        with queue_lock:
            job['status'] = 'completed'
            current_job = None
        return

    # --- PASSO 2: WebODM Automático ---
    log_messages.append(f"\n[Fila: Passo 2] Iniciando geração do Mapa WebODM (3D: {'SIM' if mesh_3d else 'NÃO'})...")
    try:
        with process_lock:
            active_process = "webodm_flow"

        # Sobe o container Docker do NodeODM
        subprocess.run(["docker", "run", "-d", "--name", "temp-nodeodm", "-p", "3000:3000", "webodm/nodeodm:stable"], capture_output=True)
        time.sleep(4)

        odm_out = out + "_webodm"
        odm_filter = 'frame_*.jpg' if mode == 'video' else 'photo_*.jpg'

        cmd_odm = [
            sys.executable, "-u", "processar_webodm.py",
            "--photos", out,
            "--out", odm_out,
            "--filter", odm_filter,
            "--quality", quality,
            "--resolution", str(resolution)
        ]
        if kmz_name:
            cmd_odm += ["--kmz-name", kmz_name]
        if mesh_3d:
            cmd_odm.append("--mesh-3d")

        with process_lock:
            active_process = subprocess.Popen(
                cmd_odm,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

        for line in iter(active_process.stdout.readline, ''):
            if line:
                log_messages.append(line.strip())
        active_process.wait()
        odm_ret = active_process.returncode

        if odm_ret == 0:
            log_messages.append(f"[Fila SUCESSO] Projeto {job_name} finalizado com êxito total!")
            with queue_lock:
                job['status'] = 'completed'
        else:
            log_messages.append(f"[Fila ERRO] WebODM finalizou com falha (código {odm_ret}).")
            with queue_lock:
                job['status'] = 'failed'

    except Exception as e:
        log_messages.append(f"[Fila ERRO WebODM]: {e}")
        with queue_lock:
            job['status'] = 'failed'
    finally:
        subprocess.run(["docker", "rm", "-f", "temp-nodeodm"], capture_output=True)
        with process_lock:
            active_process = None
        with queue_lock:
            current_job = None

def queue_worker_loop():
    """
    Loop em background que processa jobs da fila sequencialmente.
    """
    while True:
        job_to_run = None
        with queue_lock:
            for job in project_queue:
                if job.get('status') == 'pending':
                    job_to_run = job
                    break
        
        if job_to_run:
            run_project_job(job_to_run)
        else:
            time.sleep(1.0)

# Inicia thread permanente da fila
queue_worker_thread = threading.Thread(target=queue_worker_loop, daemon=True)
queue_worker_thread.start()


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


@app.route('/api/video-info', methods=['POST'])
def video_info():
    """
    Retorna metadados e duração de um arquivo de vídeo.
    """
    data = request.json or {}
    video_path = data.get('video_path', '').strip()
    if not video_path or not os.path.exists(video_path):
        return jsonify({'status': 'error', 'message': 'Arquivo de vídeo não encontrado.'}), 404
    
    try:
        from processador_video import get_video_info
        info = get_video_info(video_path)
        duration_s = info['duration']
        mins = int(duration_s // 60)
        secs = duration_s % 60
        formatted = f"{mins}m {secs:.1f}s" if mins > 0 else f"{secs:.1f}s"
        return jsonify({
            'status': 'success',
            'fps': info['fps'],
            'total_frames': info['total_frames'],
            'duration': round(duration_s, 2),
            'formatted_duration': formatted
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/queue/add', methods=['POST'])
def queue_add():
    """
    Adiciona um novo projeto à lista de espera.
    """
    data = request.json or {}
    import uuid
    job_id = str(uuid.uuid4())[:8]
    
    # Define um nome amigável para o projeto
    mode = data.get('mode', 'video')
    if mode == 'video':
        src_name = os.path.basename(data.get('video', 'Video'))
    else:
        src_name = os.path.basename(data.get('photos', 'Fotos'))
    
    out_name = os.path.basename(data.get('out', 'Destino'))
    job_name = f"[{mode.upper()}] {src_name} ➔ {out_name}"

    job = {
        'id': job_id,
        'name': job_name,
        'status': 'pending', # pending, running, completed, failed
        'created_at': time.strftime('%H:%M:%S'),
        'mode': mode,
        'video': data.get('video'),
        'srt': data.get('srt'),
        'photos': data.get('photos'),
        'out': data.get('out'),
        'interval': float(data.get('interval', 0.2)),
        'photo_interval': float(data.get('photo_interval', 2.0)),
        'filter': data.get('filter', '*.jpg'),
        'start': float(data.get('start', 0.0)),
        'end': float(data['end']) if data.get('end') is not None and str(data.get('end')).strip() != '' else None,
        'force': bool(data.get('force', False)),
        'match_datetime': bool(data.get('match_datetime', False)),
        'auto_map': bool(data.get('auto_map', True)),
        'mesh_3d': bool(data.get('mesh_3d', False)),
        'quality': data.get('quality', 'medium'),
        'resolution': float(data.get('resolution', 4.0)),
        'kmz_name': data.get('kmz_name', '')
    }

    with queue_lock:
        project_queue.append(job)

    return jsonify({'status': 'success', 'job': job})

@app.route('/api/queue/list', methods=['GET'])
def queue_list():
    """
    Lista todos os projetos na fila.
    """
    with queue_lock:
        return jsonify({
            'queue': list(project_queue),
            'current_job': current_job
        })

@app.route('/api/queue/remove', methods=['POST'])
def queue_remove():
    """
    Remove um projeto pendente da fila.
    """
    data = request.json or {}
    job_id = data.get('id')
    with queue_lock:
        global project_queue
        project_queue = [j for j in project_queue if j.get('id') != job_id or j.get('status') == 'running']
    return jsonify({'status': 'success'})

@app.route('/api/queue/clear', methods=['POST'])
def queue_clear():
    """
    Limpa todos os projetos concluídos ou pendentes que não estejam rodando.
    """
    with queue_lock:
        global project_queue
        project_queue = [j for j in project_queue if j.get('status') == 'running']
    return jsonify({'status': 'success'})

@app.route('/api/stop', methods=['POST'])
def stop_all():
    """
    Interrompe imediatamente qualquer processo ativo (georreferenciamento ou WebODM),
    remove containers Docker temporários e cancela a execução da fila.
    """
    global active_process, current_job, log_messages
    log_messages.append("\n[INTERRUPÇÃO] Solicitado cancelamento forçado pelo usuário...")
    
    # 1. Mata processo Python ativo se houver
    with process_lock:
        if active_process is not None and hasattr(active_process, 'poll'):
            try:
                active_process.terminate()
                time.sleep(0.5)
                if active_process.poll() is None:
                    active_process.kill()
                log_messages.append("[INTERRUPÇÃO] Processo de execução finalizado.")
            except Exception as e:
                log_messages.append(f"[INTERRUPÇÃO Aviso]: {e}")
        active_process = None

    # 2. Desliga container Docker do NodeODM se estiver rodando
    try:
        subprocess.run(["docker", "rm", "-f", "temp-nodeodm"], capture_output=True)
        log_messages.append("[INTERRUPÇÃO] Container Docker finalizado e liberado.")
    except Exception:
        pass

    # 3. Atualiza status do job atual e fila
    with queue_lock:
        if current_job:
            current_job['status'] = 'canceled'
            log_messages.append(f"[INTERRUPÇÃO] Projeto '{current_job.get('name')}' marcado como cancelado.")
            current_job = None

    log_messages.append("--- TUDO INTERROMPIDO COM SUCESSO ---")
    return jsonify({'status': 'success', 'message': 'Processamento cancelado com sucesso.'})


@app.route('/api/status', methods=['GET'])
def status():
    """
    Retorna o status de execução atual e itens pendentes na fila.
    """
    global active_process, current_job
    with process_lock:
        is_proc = active_process is not None
    
    with queue_lock:
        pending_count = sum(1 for j in project_queue if j.get('status') == 'pending')
        curr = current_job.get('name') if current_job else None

    return jsonify({
        'status': 'processing' if (is_proc or current_job) else 'idle',
        'current_job': curr,
        'pending_count': pending_count
    })

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
            interval = data.get('interval', 0.2)
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
        kmz_name = data.get('kmz_name', '')

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
                if kmz_name:
                    cmd += ["--kmz-name", kmz_name]
                
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



@app.route('/api/adjust-kmz', methods=['POST'])
def adjust_kmz():
    """
    Ajusta as coordenadas geográficas de um KMZ (SuperOverlay) deslocando-o em metros.
    """
    import zipfile
    import tempfile
    import shutil
    import re
    import math

    data = request.json or {}
    kmz_path = data.get('kmz_path')
    lat_shift_m = float(data.get('lat_shift_m', 0.0))
    lon_shift_m = float(data.get('lon_shift_m', 0.0))

    if not kmz_path or not os.path.exists(kmz_path):
        return jsonify({'status': 'error', 'message': 'Arquivo KMZ original não encontrado.'}), 400

    # Determinar caminho de saída (adiciona _ajustado ao nome do arquivo original se não especificado)
    dir_name = os.path.dirname(kmz_path)
    base_name = os.path.basename(kmz_path)
    name_part, ext_part = os.path.splitext(base_name)
    output_kmz_path = os.path.join(dir_name, f"{name_part}_ajustado{ext_part}")

    try:
        # 1. Determinar latitude média aproximada para conversão de metros -> graus
        avg_lat = -20.0  # valor default seguro
        with zipfile.ZipFile(kmz_path, 'r') as z:
            for name in z.namelist():
                if name.lower().endswith('.kml'):
                    content = z.read(name).decode('utf-8', errors='ignore')
                    # Tenta achar latitude ou bounding box
                    lat_match = re.search(r'<latitude>([^<]+)</latitude>', content)
                    if lat_match:
                        avg_lat = float(lat_match.group(1))
                        break
                    north_match = re.search(r'<north>([^<]+)</north>', content)
                    south_match = re.search(r'<south>([^<]+)</south>', content)
                    if north_match and south_match:
                        avg_lat = (float(north_match.group(1)) + float(south_match.group(1))) / 2.0
                        break

        # 2. Converter deslocamento em metros para graus
        lat_rad = math.radians(avg_lat)
        # Comprimento de um grau de latitude (m)
        lat_len = 111132.95 - 559.82 * math.cos(2 * lat_rad) + 1.175 * math.cos(4 * lat_rad)
        # Comprimento de um grau de longitude (m)
        lon_len = 111412.84 * math.cos(lat_rad) - 93.5 * math.cos(3 * lat_rad)

        lat_shift_deg = lat_shift_m / lat_len
        lon_shift_deg = lon_shift_m / lon_len

        # Função auxiliar de regex para shiftar as tags do KML
        def shift_kml_content(kml_text, lat_s, lon_s):
            def shift_tag(tag, shift):
                pattern = re.compile(rf'<{tag}>([^<]+)</{tag}>')
                def repl(match):
                    try:
                        val = float(match.group(1)) + shift
                        return f'<{tag}>{val:.8f}</{tag}>'
                    except ValueError:
                        return match.group(0)
                return pattern, repl

            for tag in ['north', 'south', 'latitude']:
                pattern, repl = shift_tag(tag, lat_s)
                kml_text = pattern.sub(repl, kml_text)
                
            for tag in ['east', 'west', 'longitude']:
                pattern, repl = shift_tag(tag, lon_s)
                kml_text = pattern.sub(repl, kml_text)
                
            return kml_text

        # 3. Processar em diretório temporário
        with tempfile.TemporaryDirectory() as temp_dir:
            with zipfile.ZipFile(kmz_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
                
            kml_count = 0
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file.lower().endswith('.kml'):
                        file_path = os.path.join(root, file)
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                            
                        content_updated = shift_kml_content(content, lat_shift_deg, lon_shift_deg)
                        
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(content_updated)
                        kml_count += 1

            # 4. Salvar novo KMZ
            with zipfile.ZipFile(output_kmz_path, 'w', zipfile.ZIP_DEFLATED) as zip_out:
                for root, dirs, files in os.walk(temp_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, temp_dir)
                        zip_out.write(file_path, arcname)

        return jsonify({
            'status': 'success',
            'message': f'Ajustado {kml_count} arquivos KML. Deslocamento aplicado: lat={lat_shift_deg:.8f}°, lon={lon_shift_deg:.8f}°',
            'output_path': output_kmz_path
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Erro ao processar o KMZ: {str(e)}'}), 500


@app.route('/api/generate-adjust-kmz', methods=['POST'])
def generate_adjust_kmz():
    import zipfile
    import re

    data = request.json or {}
    kmz_path = data.get('kmz_path')
    print(f"[Generate KMZ] Path received: {kmz_path}")

    if not kmz_path or not os.path.exists(kmz_path):
        print(f"[Generate KMZ] Error: path not found or empty. Path: {kmz_path}")
        return jsonify({'status': 'error', 'message': 'Arquivo KMZ original não encontrado.'}), 400

    if not os.path.isfile(kmz_path):
        print(f"[Generate KMZ] Error: path is a directory, not a file. Path: {kmz_path}")
        return jsonify({'status': 'error', 'message': 'O caminho selecionado é uma pasta. Por favor, selecione o arquivo .kmz dentro dela.'}), 400

    if not kmz_path.lower().endswith('.kmz'):
        print(f"[Generate KMZ] Error: file is not a .kmz. Path: {kmz_path}")
        return jsonify({'status': 'error', 'message': 'Por favor, selecione um arquivo com a extensão .kmz.'}), 400

    dir_name = os.path.dirname(kmz_path)
    output_preview_kmz = os.path.join(dir_name, "odm_orthophoto_ajuste_visual.kmz")

    try:
        # 1. Procurar por 0/0/0.kml ou similar no KMZ original
        print(f"[Generate KMZ] Reading ZIP file at: {kmz_path}")
        with zipfile.ZipFile(kmz_path, 'r') as z:
            names = z.namelist()
            
            # Encontra o arquivo KML da raiz do tile
            root_tile_kml_name = None
            for name in names:
                if name.endswith('0/0/0.kml'):
                    root_tile_kml_name = name
                    break
            
            if not root_tile_kml_name:
                # Fallback: pega o primeiro kml de nível mais baixo
                kmls = [n for n in names if n.lower().endswith('.kml') and n != 'doc.kml']
                if kmls:
                    kmls.sort(key=len)
                    root_tile_kml_name = kmls[0]
            
            if not root_tile_kml_name:
                return jsonify({'status': 'error', 'message': 'Não foi possível encontrar o arquivo de metadados das imagens no KMZ.'}), 400

            # Ler o KML do tile raiz para extrair as coordenadas
            tile_kml_content = z.read(root_tile_kml_name).decode('utf-8', errors='ignore')
            
            # Extrair latitude e longitude limites com cálculo de rotação para LatLonBox
            import math
            coords_match = re.search(r'<gx:LatLonQuad>\s*<coordinates>\s*([^<]+)\s*</coordinates>', tile_kml_content, re.DOTALL)
            
            if coords_match:
                pts = coords_match.group(1).strip().split()
                coords = []
                for pt in pts:
                    parts = pt.split(',')
                    if len(parts) >= 2:
                        coords.append((float(parts[0]), float(parts[1])))
                
                # gx:LatLonQuad: 0=SW, 1=SE, 2=NE, 3=NW
                sw_lon, sw_lat = coords[0]
                se_lon, se_lat = coords[1]
                ne_lon, ne_lat = coords[2]
                nw_lon, nw_lat = coords[3]
                
                center_lat = (sw_lat + se_lat + ne_lat + nw_lat) / 4.0
                center_lon = (sw_lon + se_lon + ne_lon + nw_lon) / 4.0
                
                cos_lat = math.cos(math.radians(center_lat))
                dx = (se_lon - sw_lon) * cos_lat
                dy = (se_lat - sw_lat)
                rotation_deg = math.degrees(math.atan2(dy, dx))
                
                width_deg = math.sqrt(((se_lon - sw_lon) * cos_lat) ** 2 + (se_lat - sw_lat) ** 2) / cos_lat
                height_deg = math.sqrt(((nw_lon - sw_lon) * cos_lat) ** 2 + (nw_lat - sw_lat) ** 2)
                
                north_coord = center_lat + height_deg / 2.0
                south_coord = center_lat - height_deg / 2.0
                east_coord = center_lon + width_deg / 2.0
                west_coord = center_lon - width_deg / 2.0
            else:
                north_val = re.search(r'<north>([^<]+)</north>', tile_kml_content)
                south_val = re.search(r'<south>([^<]+)</south>', tile_kml_content)
                east_val = re.search(r'<east>([^<]+)</east>', tile_kml_content)
                west_val = re.search(r'<west>([^<]+)</west>', tile_kml_content)
                if north_val and south_val and east_val and west_val:
                    north_coord = float(north_val.group(1))
                    south_coord = float(south_val.group(1))
                    east_coord = float(east_val.group(1))
                    west_coord = float(west_val.group(1))
                    rotation_deg = 0.0
                else:
                    return jsonify({'status': 'error', 'message': 'Não foi possível ler as coordenadas do KMZ.'}), 400

            # Geração da imagem de pré-visualização de ALTA RESOLUÇÃO e ALTO CONTRASTE
            from PIL import Image, ImageEnhance, ImageDraw
            import io
            import numpy as np

            preview_image_data = None
            
            # Método 1: Tentar GeoTIFF se disponível
            tif_names = ["odm_orthophoto_leve.tif", "odm_orthophoto.tif"]
            tif_path = None
            for tif_name in tif_names:
                p = os.path.join(dir_name, tif_name)
                if os.path.exists(p):
                    tif_path = p
                    break
            
            base_img = None
            if tif_path:
                try:
                    print(f"[Generate KMZ] Lendo imagem do GeoTIFF: {tif_path}")
                    with Image.open(tif_path) as img:
                        img.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
                        base_img = img.convert('RGBA')
                except Exception as ex:
                    print(f"[Generate KMZ] Erro ao ler GeoTIFF: {ex}")

            # Método 2: Montar tiles do KMZ em alta resolução (Nível 3 ou 4)
            if base_img is None:
                try:
                    png_tiles = [n for n in names if n.endswith('.png') and n != 'preview.png']
                    levels = sorted(list(set([int(n.split('/')[0]) for n in png_tiles if n.split('/')[0].isdigit()])))
                    
                    # Escolhe o nível ideal para alta resolução
                    target_lvl = 3
                    if 4 in levels:
                        target_lvl = 4
                    elif 3 in levels:
                        target_lvl = 3
                    elif levels:
                        target_lvl = max(levels)
                    
                    print(f"[Generate KMZ] Montando tiles do KMZ em Alta Resolução (Nível {target_lvl})...")
                    
                    sample_tile_name = [n for n in png_tiles if n.startswith(f'{target_lvl}/')][0]
                    sample_img = Image.open(io.BytesIO(z.read(sample_tile_name)))
                    tile_w, tile_h = sample_img.size
                    num_tiles = 2 ** target_lvl
                    
                    stitched = Image.new('RGBA', (tile_w * num_tiles, tile_h * num_tiles), (0, 0, 0, 0))
                    
                    for col in range(num_tiles):
                        for row in range(num_tiles):
                            tile_name = f"{target_lvl}/{col}/{row}.png"
                            if tile_name in names:
                                data = z.read(tile_name)
                                if len(data) > 0:
                                    timg = Image.open(io.BytesIO(data)).convert('RGBA')
                                    stitched.paste(timg, (col * tile_w, (num_tiles - 1 - row) * tile_h))
                    
                    base_img = stitched
                    print(f"[Generate KMZ] Tiles montados com sucesso! Resolução: {base_img.size[0]}x{base_img.size[1]}px")
                except Exception as ex:
                    print(f"[Generate KMZ] Erro ao montar tiles: {ex}")

            # Fallback Método 3: Imagem raiz 0/0/0.png
            if base_img is None:
                image_name = root_tile_kml_name.replace('.kml', '.png')
                if image_name in names:
                    base_img = Image.open(io.BytesIO(z.read(image_name))).convert('RGBA')

            if base_img is None:
                return jsonify({'status': 'error', 'message': 'Nenhuma imagem pôde ser extraída do KMZ.'}), 400

            # Aplicar filtro de cor falsa (MAGENTA / VIOLETA VIVO) e aumento de contraste
            arr = np.array(base_img, dtype=np.float32)
            alpha_mask = arr[:, :, 3] > 10
            
            arr[:, :, 0] = np.clip(arr[:, :, 0] * 1.5 + 50, 0, 255)
            arr[:, :, 1] = np.clip(arr[:, :, 1] * 0.15, 0, 255)
            arr[:, :, 2] = np.clip(arr[:, :, 2] * 1.3 + 40, 0, 255)
            arr[~alpha_mask, 3] = 0
            
            img_tinted = Image.fromarray(arr.astype(np.uint8), 'RGBA')
            
            enhancer = ImageEnhance.Contrast(img_tinted)
            img_tinted = enhancer.enhance(1.4)
            enhancer = ImageEnhance.Sharpness(img_tinted)
            img_tinted = enhancer.enhance(1.6)
            
            draw = ImageDraw.Draw(img_tinted)
            border_w = max(4, img_tinted.width // 300)
            for i in range(border_w):
                draw.rectangle(
                    [i, i, img_tinted.width - 1 - i, img_tinted.height - 1 - i],
                    outline=(255, 255, 0, 240)
                )
            
            img_byte_arr = io.BytesIO()
            img_tinted.save(img_byte_arr, format='PNG', optimize=True)
            preview_image_data = img_byte_arr.getvalue()
            print(f"[Generate KMZ] KMZ de ajuste gerado ({img_tinted.width}x{img_tinted.height}px - Rot={rotation_deg:.2f}°).")

        # 2. Criar o novo KMZ simplificado para ajuste visual
        doc_kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <GroundOverlay>
    <name>Mova-me no Google Earth (Ajustador)</name>
    <description>Mova esta imagem no Google Earth Pro (Botao Direito -&gt; Propriedades) e depois salve como KMZ para aplicar o ajuste no mapa completo. IMAGEM EM FALSA-COR MAGENTA para maximo contraste.</description>
    <drawOrder>999</drawOrder>
    <Icon>
      <href>preview.png</href>
    </Icon>
    <LatLonBox>
      <north>{north_coord:.8f}</north>
      <south>{south_coord:.8f}</south>
      <east>{east_coord:.8f}</east>
      <west>{west_coord:.8f}</west>
      <rotation>{rotation_deg:.8f}</rotation>
    </LatLonBox>
  </GroundOverlay>
</kml>"""

        with zipfile.ZipFile(output_preview_kmz, 'w', zipfile.ZIP_DEFLATED) as zip_out:
            zip_out.writestr("doc.kml", doc_kml_content)
            zip_out.writestr("preview.png", preview_image_data)

        return jsonify({
            'status': 'success',
            'message': 'KMZ de ajuste visual criado com sucesso!',
            'output_path': output_preview_kmz
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Erro ao gerar o KMZ de ajuste: {str(e)}'}), 500


@app.route('/api/apply-visual-adjust', methods=['POST'])
def apply_visual_adjust():
    import zipfile
    import tempfile
    import re
    import os
    import math

    data = request.json or {}
    original_kmz = data.get('original_kmz')
    adjusted_kmz = data.get('adjusted_kmz')

    if not original_kmz or not os.path.exists(original_kmz):
        return jsonify({'status': 'error', 'message': 'Arquivo KMZ original não encontrado.'}), 400
    if not os.path.isfile(original_kmz) or not original_kmz.lower().endswith('.kmz'):
        return jsonify({'status': 'error', 'message': 'O KMZ original deve ser um arquivo .kmz válido.'}), 400

    if not adjusted_kmz or not os.path.exists(adjusted_kmz):
        return jsonify({'status': 'error', 'message': 'Arquivo KMZ ajustado não encontrado.'}), 400
    if not os.path.isfile(adjusted_kmz) or not adjusted_kmz.lower().endswith('.kmz'):
        return jsonify({'status': 'error', 'message': 'O KMZ ajustado deve ser um arquivo .kmz válido.'}), 400

    try:
        # 1. Extrair os parâmetros da caixa original do WebODM (Box 0)
        with zipfile.ZipFile(original_kmz, 'r') as z:
            names = z.namelist()
            root_kml = None
            for n in names:
                if n.endswith('0/0/0.kml'):
                    root_kml = n
                    break
            if not root_kml:
                kmls = [n for n in names if n.lower().endswith('.kml') and n != 'doc.kml']
                if kmls:
                    kmls.sort(key=len)
                    root_kml = kmls[0]
            if not root_kml and 'doc.kml' in names:
                root_kml = 'doc.kml'

            if not root_kml:
                return jsonify({'status': 'error', 'message': 'KML raiz não encontrado no KMZ original.'}), 400

            txt_0 = z.read(root_kml).decode('utf-8', errors='ignore')
            coords_match = re.search(r'<gx:LatLonQuad>\s*<coordinates>\s*([^<]+)\s*</coordinates>', txt_0, re.DOTALL)
            if coords_match:
                pts = coords_match.group(1).strip().split()
                coords_0 = []
                for pt in pts:
                    parts = pt.split(',')
                    if len(parts) >= 2:
                        coords_0.append((float(parts[0]), float(parts[1])))
                sw0, se0, ne0, nw0 = coords_0[0], coords_0[1], coords_0[2], coords_0[3]
                c_lat_0 = (sw0[1] + se0[1] + ne0[1] + nw0[1]) / 4.0
                c_lon_0 = (sw0[0] + se0[0] + ne0[0] + nw0[0]) / 4.0
                cos0 = math.cos(math.radians(c_lat_0))
                dx0 = (se0[0] - sw0[0]) * cos0
                dy0 = se0[1] - sw0[1]
                rot_0_rad = math.atan2(dy0, dx0)
                w_lon_0 = math.sqrt(((se0[0] - sw0[0])*cos0)**2 + (se0[1] - sw0[1])**2) / cos0
                h_lat_0 = math.sqrt(((nw0[0] - sw0[0])*cos0)**2 + (nw0[1] - sw0[1])**2)
            else:
                nm = re.search(r'<north>([^<]+)</north>', txt_0)
                sm = re.search(r'<south>([^<]+)</south>', txt_0)
                em = re.search(r'<east>([^<]+)</east>', txt_0)
                wm = re.search(r'<west>([^<]+)</west>', txt_0)
                if nm and sm and em and wm:
                    n0, s0, e0, w0 = float(nm.group(1)), float(sm.group(1)), float(em.group(1)), float(wm.group(1))
                    c_lat_0 = (n0 + s0) / 2.0
                    c_lon_0 = (e0 + w0) / 2.0
                    w_lon_0 = e0 - w0
                    h_lat_0 = n0 - s0
                    rot_0_rad = 0.0
                    cos0 = math.cos(math.radians(c_lat_0))
                else:
                    return jsonify({'status': 'error', 'message': 'Não foi possível ler as coordenadas do KMZ original.'}), 400

        # 2. Extrair os parâmetros da caixa ajustada salva pelo Google Earth (Box 1)
        with zipfile.ZipFile(adjusted_kmz, 'r') as z:
            names = z.namelist()
            kmls = [n for n in names if n.lower().endswith('.kml')]
            if not kmls:
                return jsonify({'status': 'error', 'message': 'Nenhum KML encontrado no KMZ ajustado.'}), 400
            txt_adj = z.read(kmls[0]).decode('utf-8', errors='ignore')
            nm = re.search(r'<north>([^<]+)</north>', txt_adj)
            sm = re.search(r'<south>([^<]+)</south>', txt_adj)
            em = re.search(r'<east>([^<]+)</east>', txt_adj)
            wm = re.search(r'<west>([^<]+)</west>', txt_adj)
            if not (nm and sm and em and wm):
                return jsonify({'status': 'error', 'message': 'Coordenadas não encontradas no KMZ ajustado.'}), 400

            n1, s1, e1, w1 = float(nm.group(1)), float(sm.group(1)), float(em.group(1)), float(wm.group(1))
            rot1_m = re.search(r'<rotation>([^<]+)</rotation>', txt_adj)
            rot_1_deg = float(rot1_m.group(1)) if rot1_m else math.degrees(rot_0_rad)

        c_lat_1 = (n1 + s1) / 2.0
        c_lon_1 = (e1 + w1) / 2.0
        w_lon_1 = e1 - w1
        h_lat_1 = n1 - s1
        rot_1_rad = math.radians(rot_1_deg)
        cos1 = math.cos(math.radians(c_lat_1))

        # 3. Função de Transformação Afim completa (Translação + Escala + Rotação)
        def transform_point(lon, lat):
            u = (lon - c_lon_0) * cos0
            v = (lat - c_lat_0)
            u_loc = u * math.cos(-rot_0_rad) - v * math.sin(-rot_0_rad)
            v_loc = u * math.sin(-rot_0_rad) + v * math.cos(-rot_0_rad)
            sx = u_loc / (w_lon_0 * cos0)
            sy = v_loc / h_lat_0
            u_prime_loc = sx * (w_lon_1 * cos1)
            v_prime_loc = sy * h_lat_1
            u_prime = u_prime_loc * math.cos(rot_1_rad) - v_prime_loc * math.sin(rot_1_rad)
            v_prime = u_prime_loc * math.sin(rot_1_rad) + v_prime_loc * math.cos(rot_1_rad)
            return c_lon_1 + u_prime / cos1, c_lat_1 + v_prime

        # 4. Criar o KMZ final ajustado
        dir_name = os.path.dirname(original_kmz)
        base_name = os.path.basename(original_kmz)
        name_part, ext_part = os.path.splitext(base_name)
        output_kmz_path = os.path.join(dir_name, f"{name_part}_ajustado_visual{ext_part}")

        def transform_kml_content(kml_text):
            # A. Transformar gx:LatLonQuad
            def repl_quad(match):
                coords_str = match.group(1)
                new_pts = []
                for pt in coords_str.strip().split():
                    parts = pt.split(',')
                    if len(parts) >= 2:
                        try:
                            lon = float(parts[0])
                            lat = float(parts[1])
                            alt = parts[2] if len(parts) > 2 else '0'
                            nlon, nlat = transform_point(lon, lat)
                            new_pts.append(f'{nlon:.8f},{nlat:.8f},{alt}')
                        except ValueError:
                            new_pts.append(pt)
                    else:
                        new_pts.append(pt)
                return f'<gx:LatLonQuad>\n\t\t\t\t<coordinates>\n\t\t\t\t\t' + '\n\t\t\t\t\t'.join(new_pts) + '\n\t\t\t\t</coordinates>\n\t\t\t</gx:LatLonQuad>'

            kml_text = re.sub(r'<gx:LatLonQuad>\s*<coordinates>\s*([^<]+)\s*</coordinates>\s*</gx:LatLonQuad>', repl_quad, kml_text)

            # B. Transformar LatLonAltBox e LatLonBox
            def repl_box(match):
                tag = match.group(1)
                inner = match.group(2)
                nm = re.search(r'<north>([^<]+)</north>', inner)
                sm = re.search(r'<south>([^<]+)</south>', inner)
                em = re.search(r'<east>([^<]+)</east>', inner)
                wm = re.search(r'<west>([^<]+)</west>', inner)
                if nm and sm and em and wm:
                    try:
                        n, s, e, w = float(nm.group(1)), float(sm.group(1)), float(em.group(1)), float(wm.group(1))
                        p_sw = transform_point(w, s)
                        p_se = transform_point(e, s)
                        p_ne = transform_point(e, n)
                        p_nw = transform_point(w, n)
                        new_n = max(p_sw[1], p_se[1], p_ne[1], p_nw[1])
                        new_s = min(p_sw[1], p_se[1], p_ne[1], p_nw[1])
                        new_e = max(p_sw[0], p_se[0], p_ne[0], p_nw[0])
                        new_w = min(p_sw[0], p_se[0], p_ne[0], p_nw[0])
                        new_inner = re.sub(r'<north>[^<]+</north>', f'<north>{new_n:.8f}</north>', inner)
                        new_inner = re.sub(r'<south>[^<]+</south>', f'<south>{new_s:.8f}</south>', new_inner)
                        new_inner = re.sub(r'<east>[^<]+</east>', f'<east>{new_e:.8f}</east>', new_inner)
                        new_inner = re.sub(r'<west>[^<]+</west>', f'<west>{new_w:.8f}</west>', new_inner)
                        return f'<{tag}>{new_inner}</{tag}>'
                    except ValueError:
                        pass
                return match.group(0)

            kml_text = re.sub(r'<(LatLonAltBox|LatLonBox)>([\s\S]*?)<\/\1>', repl_box, kml_text)

            # C. Transformar Point/coordinates se houver
            def repl_point(match):
                coords_raw = match.group(1)
                new_coords = []
                for pt in coords_raw.strip().split():
                    parts = pt.split(',')
                    if len(parts) >= 2:
                        try:
                            lon = float(parts[0])
                            lat = float(parts[1])
                            alt = parts[2] if len(parts) > 2 else '0'
                            nlon, nlat = transform_point(lon, lat)
                            new_coords.append(f'{nlon:.8f},{nlat:.8f},{alt}')
                        except ValueError:
                            new_coords.append(pt)
                    else:
                        new_coords.append(pt)
                return f"<Point>\n\t\t\t\t<coordinates>{' '.join(new_coords)}</coordinates>\n\t\t\t</Point>"

            kml_text = re.sub(r'<Point>\s*<coordinates>([^<]+)</coordinates>\s*</Point>', repl_point, kml_text)
            return kml_text

        with tempfile.TemporaryDirectory() as temp_dir:
            with zipfile.ZipFile(original_kmz, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
                
            kml_count = 0
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if file.lower().endswith('.kml'):
                        file_path = os.path.join(root, file)
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            content = f.read()
                            
                        content_updated = transform_kml_content(content)
                        
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(content_updated)
                        kml_count += 1

            with zipfile.ZipFile(output_kmz_path, 'w', zipfile.ZIP_DEFLATED) as zip_out:
                for root, dirs, files in os.walk(temp_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, temp_dir)
                        zip_out.write(file_path, arcname)

        print(f"[Apply Adjust] Sucesso! Transformacao Afim aplicada em {kml_count} arquivos KML.")
        return jsonify({
            'status': 'success',
            'message': f'Ajustado {kml_count} arquivos KML com Transformação Afim (Translação, Escala e Rotação).',
            'output_path': output_kmz_path
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Erro ao processar o KMZ ajustado: {str(e)}'}), 500


@app.route('/api/generate-photos-kml', methods=['POST'])
def generate_photos_kml():
    """
    Lê o EXIF GPS de fotos em um diretório e gera um arquivo KML contendo placemarks.
    """
    import os
    import piexif
    
    data = request.json or {}
    dir_path = data.get('dir_path')
    
    if not dir_path or not os.path.exists(dir_path) or not os.path.isdir(dir_path):
        return jsonify({'status': 'error', 'message': 'Pasta de fotos inválida ou não encontrada.'}), 400

    def parse_rational(rat):
        if not rat or len(rat) < 2 or rat[1] == 0:
            return 0.0
        return float(rat[0]) / float(rat[1])

    # Encontrar fotos
    photos = [f for f in os.listdir(dir_path) if f.lower().endswith(('.jpg', '.jpeg'))]
    if not photos:
        return jsonify({'status': 'error', 'message': 'Nenhuma foto JPG/JPEG encontrada na pasta selecionada.'}), 400

    points = []
    for filename in photos:
        img_path = os.path.join(dir_path, filename)
        try:
            exif_dict = piexif.load(img_path)
            gps = exif_dict.get("GPS", {})
            if gps and piexif.GPSIFD.GPSLatitude in gps and piexif.GPSIFD.GPSLongitude in gps:
                # Converter lat
                lat_ref = gps[piexif.GPSIFD.GPSLatitudeRef].decode('ascii')
                lat_dms = gps[piexif.GPSIFD.GPSLatitude]
                lat = parse_rational(lat_dms[0]) + parse_rational(lat_dms[1])/60.0 + parse_rational(lat_dms[2])/3600.0
                if lat_ref == 'S': lat = -lat
                
                # Converter lon
                lon_ref = gps[piexif.GPSIFD.GPSLongitudeRef].decode('ascii')
                lon_dms = gps[piexif.GPSIFD.GPSLongitude]
                lon = parse_rational(lon_dms[0]) + parse_rational(lon_dms[1])/60.0 + parse_rational(lon_dms[2])/3600.0
                if lon_ref == 'W': lon = -lon
                
                # Converter altitude
                alt = 0.0
                if piexif.GPSIFD.GPSAltitude in gps:
                    alt = parse_rational(gps[piexif.GPSIFD.GPSAltitude])
                    alt_ref = gps.get(piexif.GPSIFD.GPSAltitudeRef, 0)
                    if alt_ref == 1: alt = -alt
                
                points.append((filename, lon, lat, alt))
        except Exception as e:
            print(f"Error reading EXIF from {filename}: {e}")

    if not points:
        return jsonify({'status': 'error', 'message': 'Nenhuma das fotos possui dados de posicionamento GPS válidos em seus metadados.'}), 400

    # Gerar KML
    output_kml = os.path.join(dir_path, "fotos_trajeto.kml")
    
    kml_placemarks = []
    for filename, lon, lat, alt in points:
        placemark = f"""    <Placemark>
      <name>{filename}</name>
      <Point>
        <coordinates>{lon:.8f},{lat:.8f},{alt:.1f}</coordinates>
      </Point>
    </Placemark>"""
        kml_placemarks.append(placemark)
        
    kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Trajeto de Fotos - DJI Neo</name>
    <description>Pontos de disparo das fotos georreferenciadas</description>
{chr(10).join(kml_placemarks)}
  </Document>
</kml>"""

    try:
        with open(output_kml, 'w', encoding='utf-8') as f:
            f.write(kml_content)
        return jsonify({
            'status': 'success',
            'message': f'Trajeto KML gerado com sucesso com {len(points)} pontos.',
            'output_path': output_kml
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Erro ao salvar arquivo KML: {str(e)}'}), 500


def _extract_kmz_image_and_coords(kmz_path):
    """Auxiliar para extrair a imagem de alta resolução e as 4 coordenadas [SW, SE, NE, NW] de um KMZ."""
    import zipfile
    import io
    import re
    import numpy as np
    from PIL import Image

    with zipfile.ZipFile(kmz_path, 'r') as z:
        names = z.namelist()
        root_kml = '0/0/0.kml' if '0/0/0.kml' in names else [n for n in names if n.endswith('.kml')][0]
        txt = z.read(root_kml).decode('utf-8')
        quad = re.search(r'<gx:LatLonQuad>\s*<coordinates>\s*([^<]+)', txt, re.DOTALL)
        if quad:
            pts = quad.group(1).strip().split()
            coords = []
            for p in pts:
                parts = p.split(',')
                coords.append((float(parts[0]), float(parts[1])))
            sw, se, ne, nw = coords[0], coords[1], coords[2], coords[3]
        else:
            nm = float(re.search(r'<north>([^<]+)</north>', txt).group(1))
            sm = float(re.search(r'<south>([^<]+)</south>', txt).group(1))
            em = float(re.search(r'<east>([^<]+)</east>', txt).group(1))
            wm = float(re.search(r'<west>([^<]+)</west>', txt).group(1))
            sw, se, ne, nw = (wm, sm), (em, sm), (em, nm), (wm, nm)

        png_tiles = [n for n in names if n.endswith('.png') and n != 'preview.png']
        levels = sorted(list(set([int(n.split('/')[0]) for n in png_tiles if n.split('/')[0].isdigit()])))
        
        if levels:
            target_lvl = 3 if 3 in levels else max(levels)
            sample = [n for n in png_tiles if n.startswith(f'{target_lvl}/')][0]
            s_img = Image.open(io.BytesIO(z.read(sample)))
            tw, th = s_img.size
            num_t = 2 ** target_lvl
            stitched = Image.new('RGBA', (tw * num_t, th * num_t), (0, 0, 0, 0))
            for col in range(num_t):
                for row in range(num_t):
                    tname = f'{target_lvl}/{col}/{row}.png'
                    if tname in names:
                        data = z.read(tname)
                        if len(data) > 0:
                            timg = Image.open(io.BytesIO(data)).convert('RGBA')
                            stitched.paste(timg, (col * tw, (num_t - 1 - row) * th))
            img_rgba = stitched
        elif 'preview.png' in names:
            img_rgba = Image.open(io.BytesIO(z.read('preview.png'))).convert('RGBA')
        else:
            first_png = [n for n in names if n.endswith('.png')][0]
            img_rgba = Image.open(io.BytesIO(z.read(first_png))).convert('RGBA')
            
        return np.array(img_rgba), [sw, se, ne, nw]


@app.route('/api/merge-kmz', methods=['POST'])
def merge_kmz():
    """
    Mescla 2 ou mais arquivos KMZ.
    Suporta dois modos:
      - 'seamless' (Padrão): Funde as imagens raster com homografia e suavização de bordas (feathering),
                             gerando uma imagem única contínua sem nenhum pisca-pisca (Z-fighting).
      - 'layers': Agrupa as pirâmides originais em pastas com NetworkLink.
    """
    import zipfile
    import tempfile
    import io
    import math
    import os
    import cv2
    import numpy as np
    from PIL import Image

    data = request.json or {}
    kmz_paths = data.get('kmz_paths', [])
    output_name = data.get('output_name', '').strip()
    merge_mode = data.get('mode', 'seamless')  # 'seamless' ou 'layers'

    if not kmz_paths or len(kmz_paths) < 2:
        return jsonify({'status': 'error', 'message': 'Selecione pelo menos 2 arquivos KMZ para unir.'}), 400

    valid_paths = []
    for p in kmz_paths:
        p_clean = (p or '').strip()
        if p_clean and os.path.exists(p_clean) and p_clean.lower().endswith('.kmz'):
            valid_paths.append(p_clean)

    if len(valid_paths) < 2:
        return jsonify({'status': 'error', 'message': 'É necessário fornecer pelo menos 2 arquivos .KMZ existentes válidos.'}), 400

    try:
        first_dir = os.path.dirname(valid_paths[0])
        if not output_name:
            output_name = "mapa_unificado_fundido.kmz" if merge_mode == 'seamless' else "mapa_unificado.kmz"
        if not output_name.lower().endswith('.kmz'):
            output_name += ".kmz"

        output_kmz_path = os.path.join(first_dir, output_name)

        if merge_mode == 'seamless':
            # === FUSÃO RASTER REAL (SEAMLESS MOSAIC COM FEATHERING) ===
            maps_data = []
            all_corners = []
            
            for p in valid_paths:
                arr, corners = _extract_kmz_image_and_coords(p)
                maps_data.append((arr, corners))
                all_corners.extend(corners)
                
            all_lons = [c[0] for c in all_corners]
            all_lats = [c[1] for c in all_corners]

            min_lon, max_lon = min(all_lons), max(all_lons)
            min_lat, max_lat = min(all_lats), max(all_lats)

            max_dim = 4096
            aspect = ((max_lon - min_lon) * math.cos(math.radians((min_lat + max_lat)/2.0))) / max(1e-8, (max_lat - min_lat))
            if aspect >= 1.0:
                global_w = max_dim
                global_h = max(256, int(max_dim / aspect))
            else:
                global_h = max_dim
                global_w = max(256, int(max_dim * aspect))

            accum_color = np.zeros((global_h, global_w, 3), dtype=np.float32)
            accum_weight = np.zeros((global_h, global_w), dtype=np.float32)

            def lonlat_to_pixel(lon, lat):
                x = (lon - min_lon) / (max_lon - min_lon) * (global_w - 1)
                y = (max_lat - lat) / (max_lat - min_lat) * (global_h - 1)
                return x, y

            warped_maps = []
            dist_maps = []

            for arr, (sw, se, ne, nw) in maps_data:
                h, w = arr.shape[:2]
                src_pts = np.float32([
                    [0, h - 1],
                    [w - 1, h - 1],
                    [w - 1, 0],
                    [0, 0]
                ])
                dst_pts = np.float32([
                    lonlat_to_pixel(sw[0], sw[1]),
                    lonlat_to_pixel(se[0], se[1]),
                    lonlat_to_pixel(ne[0], ne[1]),
                    lonlat_to_pixel(nw[0], nw[1])
                ])
                
                M = cv2.getPerspectiveTransform(src_pts, dst_pts)
                warped_rgba = cv2.warpPerspective(arr, M, (global_w, global_h), flags=cv2.INTER_LINEAR)
                
                alpha = warped_rgba[:, :, 3]
                valid_mask = (alpha > 10).astype(np.uint8)
                dist = cv2.distanceTransform(valid_mask, cv2.DIST_L2, 5)
                
                warped_maps.append(warped_rgba)
                dist_maps.append(dist)

            # Fusão Seamline (Voronoi) com preservação total de cores RGB:
            # Em vez de sobrepor com transparência (efeito fantasma), cada pixel
            # recebe o valor do mapa com maior confiança geométrica (maior distância da borda),
            # com transição suave em uma faixa estreita de 20px na linha de corte.
            fused_rgba = np.zeros((global_h, global_w, 4), dtype=np.uint8)

            if len(warped_maps) == 2:
                d0, d1 = dist_maps[0], dist_maps[1]
                both = (d0 > 0) & (d1 > 0)
                only0 = (d0 > 0) & (d1 == 0)
                only1 = (d1 > 0) & (d0 == 0)

                fused_rgba[only0] = warped_maps[0][only0]
                fused_rgba[only1] = warped_maps[1][only1]

                if np.any(both):
                    diff = d0[both] - d1[both]
                    w0 = np.clip((diff + 15.0) / 30.0, 0.0, 1.0)
                    w1 = 1.0 - w0
                    
                    for c in range(3):
                        fused_rgba[both, c] = np.clip(
                            warped_maps[0][both, c].astype(np.float32) * w0 +
                            warped_maps[1][both, c].astype(np.float32) * w1,
                            0, 255
                        ).astype(np.uint8)
                    fused_rgba[both, 3] = np.maximum(warped_maps[0][both, 3], warped_maps[1][both, 3])
            else:
                # 3 ou mais mapas: soma ponderada normalizada
                accum_color = np.zeros((global_h, global_w, 3), dtype=np.float32)
                accum_weight = np.zeros((global_h, global_w), dtype=np.float32)
                for i in range(len(warped_maps)):
                    w = np.power(dist_maps[i], 2)
                    for c in range(3):
                        accum_color[:, :, c] += warped_maps[i][:, :, c].astype(np.float32) * w
                    accum_weight += w
                
                valid = accum_weight > 0.001
                for c in range(3):
                    fused_rgba[valid, c] = np.clip(accum_color[valid, c] / accum_weight[valid], 0, 255).astype(np.uint8)
                fused_rgba[valid, 3] = 255

            fused_img = Image.fromarray(fused_rgba)

            doc_title = os.path.splitext(output_name)[0]
            doc_kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{doc_title}</name>
    <GroundOverlay>
      <name>{doc_title} (Mosaico Fundido)</name>
      <Icon>
        <href>files/mosaico_fundido.png</href>
      </Icon>
      <LatLonBox>
        <north>{max_lat:.8f}</north>
        <south>{min_lat:.8f}</south>
        <east>{max_lon:.8f}</east>
        <west>{min_lon:.8f}</west>
        <rotation>0.0</rotation>
      </LatLonBox>
    </GroundOverlay>
  </Document>
</kml>"""

            img_bytes = io.BytesIO()
            fused_img.save(img_bytes, format='PNG', optimize=True)

            with zipfile.ZipFile(output_kmz_path, 'w', zipfile.ZIP_DEFLATED) as zout:
                zout.writestr('doc.kml', doc_kml)
                zout.writestr('files/mosaico_fundido.png', img_bytes.getvalue())

            return jsonify({
                'status': 'success',
                'message': f'Fundidos {len(valid_paths)} mapas KMZ em um Mosaico Contínuo sem emendas e sem pisca-pisca!',
                'output_path': output_kmz_path
            })

        else:
            # === MODO CAMADAS INDIVIDUAIS (MULTI-LAYER NETWORKLINK) ===
            with tempfile.TemporaryDirectory() as temp_dir:
                folders_kml = []
                
                for idx, kmz_file in enumerate(valid_paths, start=1):
                    subfolder = f"map_{idx}"
                    sub_dir = os.path.join(temp_dir, subfolder)
                    os.makedirs(sub_dir, exist_ok=True)
                    
                    with zipfile.ZipFile(kmz_file, 'r') as z:
                        z.extractall(sub_dir)
                        
                    base_name = os.path.splitext(os.path.basename(kmz_file))[0]
                    
                    entry_kml = "doc.kml"
                    if not os.path.exists(os.path.join(sub_dir, "doc.kml")):
                        kmls = [f for f in os.listdir(sub_dir) if f.lower().endswith('.kml')]
                        if kmls:
                            entry_kml = kmls[0]
                            
                    folders_kml.append(f"""    <Folder>
      <name>{base_name}</name>
      <open>1</open>
      <NetworkLink>
        <name>{base_name}</name>
        <Link>
          <href>{subfolder}/{entry_kml}</href>
        </Link>
      </NetworkLink>
    </Folder>""")

                doc_title = os.path.splitext(output_name)[0]
                master_doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{doc_title}</name>
    <open>1</open>
{chr(10).join(folders_kml)}
  </Document>
</kml>"""

                with open(os.path.join(temp_dir, "doc.kml"), 'w', encoding='utf-8') as f_doc:
                    f_doc.write(master_doc)

                with zipfile.ZipFile(output_kmz_path, 'w', zipfile.ZIP_DEFLATED) as zip_out:
                    for root, dirs, files in os.walk(temp_dir):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, temp_dir)
                            zip_out.write(file_path, arcname)

            return jsonify({
                'status': 'success',
                'message': f'Unificados {len(valid_paths)} mapas KMZ em camadas com sucesso!',
                'output_path': output_kmz_path
            })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Erro ao unir/fundir arquivos KMZ: {str(e)}'}), 500


@app.route('/api/convert-to-lightweight-kmz', methods=['POST'])
def convert_to_lightweight_kmz():
    import zipfile
    import re
    import os

    data = request.json or {}
    kmz_path = data.get('kmz_path')

    if not kmz_path or not os.path.exists(kmz_path) or not os.path.isfile(kmz_path):
        return jsonify({'status': 'error', 'message': 'Arquivo KMZ original não encontrado.'}), 400

    dir_name = os.path.dirname(kmz_path)
    base_name = os.path.basename(kmz_path)
    name_part, ext_part = os.path.splitext(base_name)
    output_leve_kmz = os.path.join(dir_name, f"{name_part}_leve{ext_part}")

    try:
        # 1. Procurar por 0/0/0.kml ou similar no KMZ original
        with zipfile.ZipFile(kmz_path, 'r') as z:
            names = z.namelist()
            root_tile_kml_name = None
            for name in names:
                if name.endswith('0/0/0.kml'):
                    root_tile_kml_name = name
                    break
            if not root_tile_kml_name:
                kmls = [n for n in names if n.lower().endswith('.kml') and n != 'doc.kml']
                if kmls:
                    kmls.sort(key=len)
                    root_tile_kml_name = kmls[0]
            if not root_tile_kml_name:
                return jsonify({'status': 'error', 'message': 'Não foi possível encontrar metadados de imagem no KMZ.'}), 400

            tile_kml_content = z.read(root_tile_kml_name).decode('utf-8', errors='ignore')
            coords_match = re.search(r'<gx:LatLonQuad>\s*<coordinates>\s*([^<]+)\s*</coordinates>', tile_kml_content, re.DOTALL)
            
            lons = []
            lats = []
            if coords_match:
                points = coords_match.group(1).strip().split()
                for pt in points:
                    parts = pt.split(',')
                    if len(parts) >= 2:
                        lons.append(float(parts[0]))
                        lats.append(float(parts[1]))
            else:
                north_val = re.search(r'<north>([^<]+)</north>', tile_kml_content)
                south_val = re.search(r'<south>([^<]+)</south>', tile_kml_content)
                east_val = re.search(r'<east>([^<]+)</east>', tile_kml_content)
                west_val = re.search(r'<west>([^<]+)</west>', tile_kml_content)
                if north_val and south_val and east_val and west_val:
                    lats = [float(north_val.group(1)), float(south_val.group(1))]
                    lons = [float(east_val.group(1)), float(west_val.group(1))]

            if lons and lats:
                north_coord = max(lats)
                south_coord = min(lats)
                east_coord = max(lons)
                west_coord = min(lons)
            else:
                return jsonify({'status': 'error', 'message': 'Não foi possível ler as coordenadas do KMZ.'}), 400

            preview_image_data = None
            tif_names = ["odm_orthophoto_leve.tif", "odm_orthophoto.tif"]
            for tif_name in tif_names:
                p = os.path.join(dir_name, tif_name)
                if os.path.exists(p):
                    try:
                        from PIL import Image
                        import io
                        with Image.open(p) as img:
                            img.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                            img_byte_arr = io.BytesIO()
                            img.save(img_byte_arr, format='PNG')
                            preview_image_data = img_byte_arr.getvalue()
                            break
                    except Exception:
                        pass

            if not preview_image_data:
                image_name = root_tile_kml_name.replace('.kml', '.png')
                if image_name not in names:
                    png_files = [n for n in names if n.lower().endswith('.png')]
                    if png_files:
                        image_name = png_files[0]
                preview_image_data = z.read(image_name)

        doc_kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <GroundOverlay>
    <name>{name_part}_leve</name>
    <description>KMZ Leve Otimizado de Imagem Única</description>
    <drawOrder>999</drawOrder>
    <Icon>
      <href>preview.png</href>
    </Icon>
    <LatLonBox>
      <north>{north_coord:.8f}</north>
      <south>{south_coord:.8f}</south>
      <east>{east_coord:.8f}</east>
      <west>{west_coord:.8f}</west>
    </LatLonBox>
  </GroundOverlay>
</kml>"""

        with zipfile.ZipFile(output_leve_kmz, 'w', zipfile.ZIP_DEFLATED) as zip_out:
            zip_out.writestr("doc.kml", doc_kml_content)
            zip_out.writestr("preview.png", preview_image_data)

        return jsonify({
            'status': 'success',
            'message': 'KMZ leve gerado com sucesso!',
            'output_path': output_leve_kmz
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Erro ao gerar o KMZ leve: {str(e)}'}), 500


@app.route('/api/photo-to-kmz', methods=['POST'])
def photo_to_kmz():
    import os
    import zipfile
    import piexif
    import math

    data = request.json or {}
    img_path = data.get('img_path')

    if not img_path or not os.path.exists(img_path) or not os.path.isfile(img_path):
        return jsonify({'status': 'error', 'message': 'Arquivo de imagem não encontrado.'}), 400

    if not img_path.lower().endswith(('.jpg', '.jpeg')):
        return jsonify({'status': 'error', 'message': 'Por favor, selecione uma foto no formato JPG ou JPEG.'}), 400

    def parse_rational(rat):
        if not rat or len(rat) < 2 or rat[1] == 0:
            return 0.0
        return float(rat[0]) / float(rat[1])

    try:
        # 1. Carregar coordenadas EXIF GPS da foto
        exif_dict = piexif.load(img_path)
        gps = exif_dict.get("GPS", {})
        if not gps or piexif.GPSIFD.GPSLatitude not in gps or piexif.GPSIFD.GPSLongitude not in gps:
            return jsonify({'status': 'error', 'message': 'Esta foto não possui coordenadas GPS válidas nos metadados EXIF.'}), 400

        # Latitude
        lat_ref = gps[piexif.GPSIFD.GPSLatitudeRef].decode('ascii')
        lat_dms = gps[piexif.GPSIFD.GPSLatitude]
        lat = parse_rational(lat_dms[0]) + parse_rational(lat_dms[1])/60.0 + parse_rational(lat_dms[2])/3600.0
        if lat_ref == 'S': lat = -lat
        
        # Longitude
        lon_ref = gps[piexif.GPSIFD.GPSLongitudeRef].decode('ascii')
        lon_dms = gps[piexif.GPSIFD.GPSLongitude]
        lon = parse_rational(lon_dms[0]) + parse_rational(lon_dms[1])/60.0 + parse_rational(lon_dms[2])/3600.0
        if lon_ref == 'W': lon = -lon
        
        # Altitude (se houver, senão assume 40m de padrão de voo)
        alt = 40.0
        if piexif.GPSIFD.GPSAltitude in gps:
            alt_val = parse_rational(gps[piexif.GPSIFD.GPSAltitude])
            if alt_val > 0:
                alt = alt_val
                
        # 2. Calcular limites da projeção baseados na altura do voo
        # Assumindo FOV de ~85 graus horizontal
        fov_h_rad = math.radians(85.0)
        fov_v_rad = math.radians(65.0)
        
        width_m = 2.0 * alt * math.tan(fov_h_rad / 2.0)
        height_m = 2.0 * alt * math.tan(fov_v_rad / 2.0)
        
        # Converter metros para graus
        lat_rad = math.radians(lat)
        lat_len = 111132.95 - 559.82 * math.cos(2 * lat_rad) + 1.175 * math.cos(4 * lat_rad)
        lon_len = 111412.84 * math.cos(lat_rad) - 93.5 * math.cos(3 * lat_rad)
        
        lat_delta = (height_m / 2.0) / lat_len
        lon_delta = (width_m / 2.0) / lon_len
        
        north = lat + lat_delta
        south = lat - lat_delta
        east = lon + lon_delta
        west = lon - lon_delta

        # Diretorio e nomes de saida
        dir_name = os.path.dirname(img_path)
        base_name = os.path.basename(img_path)
        name_part, ext_part = os.path.splitext(base_name)
        output_kmz = os.path.join(dir_name, f"{name_part}.kmz")

        # 3. Gerar KML
        doc_kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <GroundOverlay>
    <name>{name_part}</name>
    <description>Foto de Drone projetada via GPS. Altitude de voo estimada: {alt:.1f}m.</description>
    <drawOrder>10</drawOrder>
    <Icon>
      <href>imagem.jpg</href>
    </Icon>
    <LatLonBox>
      <north>{north:.8f}</north>
      <south>{south:.8f}</south>
      <east>{east:.8f}</east>
      <west>{west:.8f}</west>
    </LatLonBox>
  </GroundOverlay>
</kml>"""

        # 4. Criar o KMZ empacotando o KML e copiando a foto
        with zipfile.ZipFile(output_kmz, 'w', zipfile.ZIP_DEFLATED) as zip_out:
            zip_out.writestr("doc.kml", doc_kml_content)
            zip_out.write(img_path, "imagem.jpg")

        return jsonify({
            'status': 'success',
            'message': f'KMZ gerado com sucesso para a foto {base_name}. Coordenadas: {lat:.6f}, {lon:.6f}',
            'output_path': output_kmz
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Erro ao gerar o KMZ da foto: {str(e)}'}), 500


@app.route('/viewer-3d')





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

@app.route('/api/3d-model')
def get_3d_model():
    """
    Serve o arquivo GLB do modelo 3D.
    """
    model_path = request.args.get('path', r"C:\Users\mkas2\Desktop\talhao\teste\Resultado_webodm\odm_texturing\odm_textured_model_geo.glb")
    if os.path.exists(model_path):
        return send_file(model_path, mimetype='model/gltf-binary', conditional=True)
    else:
        return jsonify({'error': 'Modelo 3D não encontrado'}), 404

@app.route('/api/odm-static/<path:filename>')
def serve_odm_static(filename):
    """
    Serve arquivos estáticos da pasta do WebODM (OBJ, MTL, Imagens de textura).
    """
    directory = r"C:\Users\mkas2\Desktop\talhao\teste\Resultado_webodm\odm_texturing"
    return send_from_directory(directory, filename)

if __name__ == '__main__':
    # Roda o servidor Flask localmente na porta 5001 para burlar o cache
    app.run(host='0.0.0.0', port=5001, debug=False)
