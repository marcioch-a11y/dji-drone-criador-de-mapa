import os
import sys
import time
import subprocess
import threading
import uuid
from flask import Blueprint, request, jsonify
import state

queue_bp = Blueprint('queue_bp', __name__)


def run_project_job(job):
    """
    Executa um projeto completo da fila:
    Passo 1: Georreferenciamento (main.py)
    Passo 2 (opcional): Processamento WebODM / Orquestrador Docker (processar_webodm.py)
    """
    job_id = job.get('id')
    job_name = job.get('name', 'Projeto')
    
    with state.queue_lock:
        job['status'] = 'running'
        state.current_job = job

    state.add_log("\n=======================================================")
    state.add_log(f"[Fila de Projetos] INICIANDO: {job_name}")
    state.add_log("=======================================================")

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

    state.add_log("[Fila: Passo 1] Extraindo e georreferenciando fotos...")
    try:
        with state.process_lock:
            state.active_process = subprocess.Popen(
                cmd_geo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
        
        for line in iter(state.active_process.stdout.readline, ''):
            if line:
                state.add_log(line.strip())
        state.active_process.wait()
        geo_ret = state.active_process.returncode
    except Exception as e:
        state.add_log(f"[ERRO no Georreferenciamento]: {e}")
        geo_ret = -1
    finally:
        with state.process_lock:
            state.active_process = None

    if geo_ret != 0:
        state.add_log(f"[Fila AVISO] Georreferenciamento finalizou com código {geo_ret}. Abortando mapa para este projeto.")
        with state.queue_lock:
            job['status'] = 'failed'
            state.current_job = None
        return

    state.add_log(f"[Fila: Passo 1 Concluído] Fotos salvas com sucesso em: {out}")

    # Se auto_map não estiver ativado, conclui aqui
    if not auto_map:
        state.add_log(f"[Fila] Projeto {job_name} concluído (Criação de Mapa WebODM estava desativada).")
        with state.queue_lock:
            job['status'] = 'completed'
            state.current_job = None
        return

    # --- PASSO 2: WebODM Automático ---
    state.add_log(f"\n[Fila: Passo 2] Iniciando geração do Mapa WebODM (3D: {'SIM' if mesh_3d else 'NÃO'})...")
    try:
        with state.process_lock:
            state.active_process = "webodm_flow"

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

        with state.process_lock:
            state.active_process = subprocess.Popen(
                cmd_odm,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

        for line in iter(state.active_process.stdout.readline, ''):
            if line:
                state.add_log(line.strip())
        state.active_process.wait()
        odm_ret = state.active_process.returncode

        if odm_ret == 0:
            state.add_log(f"[Fila SUCESSO] Projeto {job_name} finalizado com êxito total!")
            with state.queue_lock:
                job['status'] = 'completed'
        else:
            state.add_log(f"[Fila ERRO] WebODM finalizou com falha (código {odm_ret}).")
            with state.queue_lock:
                job['status'] = 'failed'

    except Exception as e:
        state.add_log(f"[Fila ERRO WebODM]: {e}")
        with state.queue_lock:
            job['status'] = 'failed'
    finally:
        subprocess.run(["docker", "rm", "-f", "temp-nodeodm"], capture_output=True)
        with state.process_lock:
            state.active_process = None
        with state.queue_lock:
            state.current_job = None


def queue_worker_loop():
    """
    Loop em background que processa jobs da fila sequencialmente.
    """
    while True:
        job_to_run = None
        with state.queue_lock:
            for job in state.project_queue:
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


@queue_bp.route('/api/queue/add', methods=['POST'])
def queue_add():
    """
    Adiciona um novo projeto à lista de espera.
    """
    data = request.json or {}
    job_id = str(uuid.uuid4())[:8]
    
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
        'status': 'pending',
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

    with state.queue_lock:
        state.project_queue.append(job)

    return jsonify({'status': 'success', 'job': job})


@queue_bp.route('/api/queue/list', methods=['GET'])
def queue_list():
    """
    Lista todos os projetos na fila.
    """
    with state.queue_lock:
        return jsonify({
            'queue': list(state.project_queue),
            'current_job': state.current_job
        })


@queue_bp.route('/api/queue/remove', methods=['POST'])
def queue_remove():
    """
    Remove um projeto pendente da fila.
    """
    data = request.json or {}
    job_id = data.get('id')
    with state.queue_lock:
        state.project_queue = [j for j in state.project_queue if j.get('id') != job_id or j.get('status') == 'running']
    return jsonify({'status': 'success'})


@queue_bp.route('/api/queue/clear', methods=['POST'])
def queue_clear():
    """
    Limpa todos os projetos concluídos ou pendentes que não estejam rodando.
    """
    with state.queue_lock:
        state.project_queue = [j for j in state.project_queue if j.get('status') == 'running']
    return jsonify({'status': 'success'})
