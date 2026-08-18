import os
import sys
import time
import subprocess
import threading
from flask import Blueprint, request, jsonify, Response
import state

pipeline_bp = Blueprint('pipeline_bp', __name__)


def read_process_output(proc):
    """
    Thread que consome a saída padrão de um processo em tempo real e a direciona para a fila de logs.
    """
    try:
        for line in iter(proc.stdout.readline, ''):
            if line:
                state.add_log(line.strip())
        proc.wait()
    except Exception as e:
        state.add_log(f"[ERRO] Falha ao ler saída do processo: {e}")
    finally:
        with state.process_lock:
            state.active_process = None
        state.add_log("--- PROCESSO FINALIZADO ---")


@pipeline_bp.route('/api/video-info', methods=['POST'])
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


@pipeline_bp.route('/api/stop', methods=['POST'])
def stop_all():
    """
    Interrompe imediatamente qualquer processo ativo (georreferenciamento ou WebODM),
    remove containers Docker temporários e cancela a execução da fila.
    """
    state.add_log("\n[INTERRUPÇÃO] Solicitado cancelamento forçado pelo usuário...")
    
    # 1. Mata processo Python ativo se houver
    with state.process_lock:
        if state.active_process is not None and hasattr(state.active_process, 'poll'):
            try:
                state.active_process.terminate()
                time.sleep(0.5)
                if state.active_process.poll() is None:
                    state.active_process.kill()
                state.add_log("[INTERRUPÇÃO] Processo de execução finalizado.")
            except Exception as e:
                state.add_log(f"[INTERRUPÇÃO Aviso]: {e}")
        state.active_process = None

    # 2. Desliga container Docker do NodeODM se estiver rodando
    try:
        subprocess.run(["docker", "rm", "-f", "temp-nodeodm"], capture_output=True)
        state.add_log("[INTERRUPÇÃO] Container Docker finalizado e liberado.")
    except Exception:
        pass

    # 3. Atualiza status do job atual e fila
    with state.queue_lock:
        if state.current_job:
            state.current_job['status'] = 'canceled'
            state.add_log(f"[INTERRUPÇÃO] Projeto '{state.current_job.get('name')}' marcado como cancelado.")
            state.current_job = None

    state.add_log("--- TUDO INTERROMPIDO COM SUCESSO ---")
    return jsonify({'status': 'success', 'message': 'Processamento cancelado com sucesso.'})


@pipeline_bp.route('/api/status', methods=['GET'])
def status():
    """
    Retorna o status de execução atual e itens pendentes na fila.
    """
    with state.process_lock:
        is_proc = state.active_process is not None
    
    with state.queue_lock:
        pending_count = sum(1 for j in state.project_queue if j.get('status') == 'pending')
        curr = state.current_job.get('name') if state.current_job else None

    return jsonify({
        'status': 'processing' if (is_proc or state.current_job) else 'idle',
        'current_job': curr,
        'pending_count': pending_count
    })


@pipeline_bp.route('/api/logs', methods=['GET'])
def logs():
    """
    Endpoint de Server-Sent Events (SSE) para transmissão dos logs em tempo real.
    """
    def event_stream():
        idx = 0
        while True:
            if idx < len(state.log_messages):
                yield f"data: {state.log_messages[idx]}\n\n"
                idx += 1
            else:
                time.sleep(0.2)
    return Response(event_stream(), mimetype="text/event-stream")


@pipeline_bp.route('/api/run-pipeline', methods=['POST'])
def run_pipeline():
    """
    Endpoint para iniciar a extração e injeção de coordenadas GPS.
    """
    with state.process_lock:
        if state.active_process is not None:
            return jsonify({'status': 'error', 'message': 'Já existe uma tarefa em execução no servidor.'}), 400

        data = request.json or {}
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
        state.clear_logs("--- INICIANDO PIPELINE DE GEORREFERENCIAMENTO ---")
        print(f"[App] Executando comando: {' '.join(cmd)}")

        try:
            state.active_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            thread = threading.Thread(target=read_process_output, args=(state.active_process,))
            thread.daemon = True
            thread.start()
            return jsonify({'status': 'started'})
        except Exception as e:
            state.active_process = None
            return jsonify({'status': 'error', 'message': f'Erro ao iniciar subprocesso: {str(e)}'}), 500


@pipeline_bp.route('/api/generate-kml', methods=['POST'])
def generate_kml():
    """
    Lê os dados EXIF GPS das fotos de um diretório e gera um arquivo KML para visualização 3D da rota no Google Earth.
    """
    import piexif

    data = request.json or {}
    dir_path = data.get('dir')
    if not dir_path or not os.path.exists(dir_path):
        return jsonify({'status': 'error', 'message': 'Diretório de fotos não encontrado.'}), 400

    def parse_rational(rat):
        if not rat or len(rat) < 2 or rat[1] == 0:
            return 0.0
        return float(rat[0]) / float(rat[1])

    points = []
    for filename in sorted(os.listdir(dir_path)):
        if not filename.lower().endswith(('.jpg', '.jpeg')):
            continue
        file_path = os.path.join(dir_path, filename)
        try:
            exif_dict = piexif.load(file_path)
            gps = exif_dict.get("GPS", {})
            if gps and piexif.GPSIFD.GPSLatitude in gps and piexif.GPSIFD.GPSLongitude in gps:
                lat_ref = gps[piexif.GPSIFD.GPSLatitudeRef].decode('ascii')
                lat_dms = gps[piexif.GPSIFD.GPSLatitude]
                lat = parse_rational(lat_dms[0]) + parse_rational(lat_dms[1])/60.0 + parse_rational(lat_dms[2])/3600.0
                if lat_ref == 'S': lat = -lat
                
                lon_ref = gps[piexif.GPSIFD.GPSLongitudeRef].decode('ascii')
                lon_dms = gps[piexif.GPSIFD.GPSLongitude]
                lon = parse_rational(lon_dms[0]) + parse_rational(lon_dms[1])/60.0 + parse_rational(lon_dms[2])/3600.0
                if lon_ref == 'W': lon = -lon
                
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
