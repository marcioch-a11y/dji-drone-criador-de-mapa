import cv2
import numpy as np

def gerar_video_mock(filepath, duration=6.0, fps=30, width=640, height=480):
    """
    Gera um arquivo de vídeo MP4 sintético de teste usando OpenCV.
    """
    print(f"Gerando vídeo mock: {filepath} ({duration}s, {fps} FPS)...")
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(filepath, fourcc, fps, (width, height))
    
    total_frames = int(duration * fps)
    for frame_idx in range(total_frames):
        # Cria uma matriz de pixels pretos
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        
        # Cor de fundo dinâmica
        frame[:] = [int(50 + (frame_idx % 100)), 80, int(150 - (frame_idx % 50))]
        
        # Desenha um círculo que se move na tela
        center_x = int((frame_idx * 5) % width)
        center_y = int(height / 2)
        cv2.circle(frame, (center_x, center_y), 40, (255, 255, 255), -1)
        
        # Insere o indicador de tempo no frame para depuração visual
        t = frame_idx / fps
        cv2.putText(frame, f"TESTE DJI NEO t={t:.2f}s", (20, 50), 
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
        cv2.putText(frame, f"Frame: {frame_idx}", (20, 90), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 1)
        
        out.write(frame)
        
    out.release()
    print("   -> Vídeo mock criado com sucesso.")

def gerar_srt_mock(filepath, duration=6.0):
    """
    Gera um arquivo SRT de legenda sintético contendo telemetria GPS simulada
    compatível com os padrões de metadados da DJI.
    """
    print(f"Gerando telemetria SRT mock: {filepath}...")
    with open(filepath, 'w', encoding='utf-8') as f:
        for sec in range(int(duration)):
            start_ms = sec * 1000
            end_ms = (sec + 1) * 1000
            
            # Formatação de tempo padrão SRT (HH:MM:SS,mmm)
            h_start, m_start, s_start = start_ms // 3600000, (start_ms % 3600000) // 60000, (start_ms % 60000) // 1000
            ms_start = start_ms % 1000
            
            h_end, m_end, s_end = end_ms // 3600000, (end_ms % 3600000) // 60000, (end_ms % 60000) // 1000
            ms_end = end_ms % 1000
            
            start_str = f"{h_start:02d}:{m_start:02d}:{s_start:02d},{ms_start:03d}"
            end_str = f"{h_end:02d}:{m_end:02d}:{s_end:02d},{ms_end:03d}"
            
            # Coordenadas geográficas simuladas com variação gradual (Drone voando)
            # Região aproximada de São Paulo - SP
            lat = -23.550520 - (sec * 0.000150)
            lon = -46.633308 + (sec * 0.000110)
            alt = 760.5 + (sec * 1.25)
            
            f.write(f"{sec + 1}\n")
            f.write(f"{start_str} --> {end_str}\n")
            f.write(f"FrameCnt : {(sec+1)*30}, DiffTime : 1000ms\n")
            # Linha estruturada semelhante ao SRT do DJI Neo
            f.write(f"2026-06-13 13:58:{sec:02d},000 [iso : 100] [shutter : 1/200] [fnum : 280] [latitude : {lat:.6f}] [longtitude : {lon:.6f}] [altitude: {alt:.2f}]\n\n")
            
    print("   -> SRT mock criado com sucesso.")

if __name__ == "__main__":
    gerar_video_mock("mock_video.mp4")
    gerar_srt_mock("mock_telemetria.srt")
