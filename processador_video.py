import cv2
import os

def get_video_info(video_path):
    """
    Retorna informações básicas do arquivo de vídeo: FPS, total de frames e duração em segundos.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Não foi possível abrir o arquivo de vídeo: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps > 0 else 0.0
    cap.release()

    return {
        'fps': fps,
        'total_frames': total_frames,
        'duration': duration
    }

def extrair_frames(video_path, interval=1.5, start_time=0.0, end_time=None):
    """
    Um gerador que lê o vídeo e extrai frames espaçados pelo intervalo temporal informado (em segundos)
    dentro da janela delimitada por [start_time, end_time].
    Garante o alinhamento temporal correto definindo a posição do frame pelo índice (com base no FPS).
    
    Yields:
        tuple: (frame_matriz, timestamp_segundos)
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Não foi possível abrir o arquivo de vídeo: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps > 0 else 0.0

    # Define a janela de tempo
    final_time = min(duration, end_time) if end_time is not None else duration

    t = max(0.0, start_time)
    while t <= final_time:
        # Calcula o índice exato do frame correspondente ao tempo t
        frame_idx = int(round(t * fps))
        if frame_idx >= total_frames:
            break

        # Define a propriedade do leitor para o frame calculado
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if not ret:
            # Tenta ler o frame subsequente caso ocorra falha de busca no codec
            ret, frame = cap.read()
            if not ret:
                break

        yield frame, t
        t += interval

    cap.release()
