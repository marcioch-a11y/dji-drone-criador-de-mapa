import os
import sys
import argparse
import cv2

# Importação dos módulos locais
import leitor_srt
import processador_video
import injetor_exif

def validar_alinhamento(video_info, telemetry, limite_dif=3.0, limite_inicio=1.5):
    """
    Valida se o arquivo SRT e o vídeo possuem o mesmo alinhamento de tempo inicial e duração compatível.
    """
    if not telemetry:
        raise ValueError("O arquivo SRT está vazio ou não possui telemetrias GPS estruturadas identificáveis.")

    srt_start = telemetry[0]['start_time']
    srt_end = telemetry[-1]['end_time']
    video_duration = video_info['duration']

    # 1. Verifica alinhamento de início
    if srt_start > limite_inicio:
        raise ValueError(
            f"Erro de Alinhamento Inicial: A primeira telemetria no SRT começa em {srt_start:.2f}s. "
            f"Espera-se que comece em até {limite_inicio:.2f}s para garantir que vídeo e SRT iniciaram juntos."
        )

    # 2. Verifica compatibilidade de duração total
    diferenca_duracao = abs(video_duration - srt_end)
    if diferenca_duracao > limite_dif:
        raise ValueError(
            f"Erro de Alinhamento de Duração: A duração total do vídeo ({video_duration:.2f}s) "
            f"e o tempo da última telemetria SRT ({srt_end:.2f}s) diferem em {diferenca_duracao:.2f}s. "
            f"O limite aceitável de tolerância é {limite_dif:.2f}s."
        )

def obter_telemetria_mais_proxima(t, telemetry):
    """
    Retorna a entrada de telemetria mais próxima no tempo para o instante t.
    """
    if not telemetry:
        return None
    # Busca a telemetria minimizando a distância absoluta entre o instante t e o ponto médio do intervalo da telemetria
    return min(telemetry, key=lambda x: abs(((x['start_time'] + x['end_time']) / 2.0) - t))

import re
import shutil

def parse_filename_datetime(filename):
    """
    Tenta extrair data e hora a partir de um padrão de 14 dígitos (YYYYMMDDHHMMSS) no nome do arquivo.
    """
    match = re.search(r'(\d{4})(\d{2})(\d{2})_?(\d{2})(\d{2})(\d{2})', filename)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)} {match.group(4)}:{match.group(5)}:{match.group(6)}"
    return None

def main():
    parser = argparse.ArgumentParser(
        description="Pipeline de Preparação de Imagens para Mapeamento a partir de Drone DJI Neo."
    )
    # Entrada mutually exclusive (tecnicamente)
    parser.add_argument("--video", help="Caminho do arquivo de vídeo (.MP4) para extrair frames")
    parser.add_argument("--photos", help="Caminho do diretório contendo fotos (.JPG) existentes")
    parser.add_argument("--srt", required=True, help="Caminho do arquivo de telemetria (.SRT)")
    parser.add_argument("--out", required=True, help="Diretório de saída para os frames/fotos processadas")
    parser.add_argument("--interval", type=float, default=1.5, help="Intervalo de extração de frames do vídeo (segundos). Padrão: 1.5s")
    parser.add_argument("--photo-interval", type=float, default=2.0, help="Intervalo temporal presumido entre fotos no modo sequencial (segundos). Padrão: 2.0s")
    parser.add_argument("--match-datetime", action="store_true", help="Tenta cruzar fotos e telemetria usando a data/hora absoluta do nome do arquivo")
    parser.add_argument("--force", action="store_true", help="Ignora erros de validação de alinhamento temporal e força o processamento")
    parser.add_argument("--start", type=float, default=0.0, help="Tempo de início para corte do processamento (segundos). Padrão: 0.0s")
    parser.add_argument("--end", type=float, default=None, help="Tempo de fim para corte do processamento (segundos). Padrão: fim do vídeo")

    args = parser.parse_args()

    # Validação mútua de entradas
    if not args.video and not args.photos:
        print("Erro: É necessário especificar um modo de entrada: --video <caminho> ou --photos <pasta>", file=sys.stderr)
        sys.exit(1)
    if args.video and args.photos:
        print("Erro: Especifique apenas uma entrada: --video OU --photos (não ambos)", file=sys.stderr)
        sys.exit(1)

    # Validação de existência de arquivos/pastas de entrada
    if args.video and not os.path.isfile(args.video):
        print(f"Erro: O arquivo de vídeo '{args.video}' não foi encontrado.", file=sys.stderr)
        sys.exit(1)
    if args.photos and not os.path.isdir(args.photos):
        print(f"Erro: O diretório de fotos '{args.photos}' não foi encontrado.", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(args.srt):
        print(f"Erro: O arquivo SRT '{args.srt}' não foi encontrado.", file=sys.stderr)
        sys.exit(1)

    # Validações dos limites de tempo informados
    if args.start < 0:
        print("Erro: O tempo de início (--start) não pode ser negativo.", file=sys.stderr)
        sys.exit(1)
    if args.end is not None and args.end <= args.start:
        print("Erro: O tempo de fim (--end) deve ser maior do que o tempo de início (--start).", file=sys.stderr)
        sys.exit(1)

    print("--- INICIANDO PROCESSAMENTO DO PIPELINE ---")
    
    # 1. Parsear o arquivo SRT
    print("[1/4] Lendo e parseando arquivo de telemetria .SRT...")
    try:
        telemetry = leitor_srt.parse_srt(args.srt)
        print(f"   -> Sucesso! Encontrados {len(telemetry)} pontos de telemetria GPS válidos.")
    except Exception as e:
        print(f"Erro ao analisar o arquivo SRT: {e}", file=sys.stderr)
        sys.exit(1)

    # Criar diretório de saída
    os.makedirs(args.out, exist_ok=True)

    if args.video:
        # --- MODO VÍDEO ---
        # 2. Ler metadados do vídeo
        print("[2/4] Lendo propriedades do arquivo de vídeo...")
        try:
            video_info = processador_video.get_video_info(args.video)
            print(f"   -> Vídeo: {video_info['fps']:.2f} FPS | Duração: {video_info['duration']:.2f}s | total frames: {video_info['total_frames']}")
        except Exception as e:
            print(f"Erro ao ler informações do vídeo: {e}", file=sys.stderr)
            sys.exit(1)

        # 3. Validar o alinhamento temporal inicial e final
        print("[3/4] Validando alinhamento temporal entre vídeo e telemetria...")
        try:
            validar_alinhamento(video_info, telemetry)
            print("   -> Alinhamento validado com sucesso!")
        except ValueError as ve:
            if args.force:
                print(f"   [AVISO] Falha de validação de alinhamento: {ve}", file=sys.stderr)
                print("   -> Procedendo sob a opção '--force' (pode haver desalinhamento de georreferenciamento!).")
            else:
                print(f"   [ERRO] Falha de validação de alinhamento: {ve}", file=sys.stderr)
                print("   -> Use a flag '--force' se desejar rodar o pipeline mesmo assim.", file=sys.stderr)
                sys.exit(1)

        # 4. Extração e injeção EXIF
        print(f"[4/4] Processando e georreferenciando frames (Janela: {args.start:.2f}s até "
              f"{f'{args.end:.2f}s' if args.end is not None else 'fim do vídeo'})...")
        
        frame_count = 0
        generator = processador_video.extrair_frames(
            args.video, 
            interval=args.interval, 
            start_time=args.start, 
            end_time=args.end
        )

        try:
            for frame, t in generator:
                frame_count += 1
                ponto_gps = obter_telemetria_mais_proxima(t, telemetry)
                
                if not ponto_gps:
                    print(f"   [AVISO] Nenhuma telemetria encontrada para o instante t={t:.2f}s. Pulando frame.")
                    continue

                filename = f"frame_{frame_count:04d}_t_{t:.2f}.jpg"
                img_path = os.path.join(args.out, filename)
                cv2.imwrite(img_path, frame)

                lat = ponto_gps['latitude']
                lon = ponto_gps['longitude']
                alt = ponto_gps['altitude']
                
                injetor_exif.injetar_gps(img_path, lat, lon, alt)
                print(f"Frame {frame_count} processado - Lat: {lat:.6f}, Lon: {lon:.6f}, Alt: {alt:.1f}m (t={t:.2f}s)")
                
        except Exception as e:
            print(f"Erro de processamento durante a execução: {e}", file=sys.stderr)
            sys.exit(1)

    else:
        # --- MODO FOTO ---
        # 2. Carregar arquivos de imagem da pasta
        print("[2/4] Buscando imagens na pasta especificada...")
        valid_extensions = ('.jpg', '.jpeg', '.png')
        photo_files = sorted([
            os.path.join(args.photos, f) for f in os.listdir(args.photos)
            if f.lower().endswith(valid_extensions)
        ])
        print(f"   -> Encontradas {len(photo_files)} imagens válidas.")
        if not photo_files:
            print("Erro: Nenhuma imagem (.jpg, .jpeg, .png) encontrada na pasta especificada.", file=sys.stderr)
            sys.exit(1)

        # 3. Mapear fotos com base em data/hora ou sequência
        print("[3/4] Mapeando e copiando fotos georreferenciadas...")
        frame_count = 0

        # Mapeamento rápido por data/hora se a flag estiver ativa
        telemetry_by_dt = {}
        if args.match_datetime:
            for pt in telemetry:
                if pt.get('datetime'):
                    # O formato no SRT pode ter frações de segundo, normaliza para YYYY-MM-DD HH:MM:SS
                    dt_normalized = pt['datetime'].split(',')[0].split('.')[0].strip()
                    telemetry_by_dt[dt_normalized] = pt

        for idx, photo_path in enumerate(photo_files):
            filename = os.path.basename(photo_path)
            ponto_gps = None
            t_mapped = None

            if args.match_datetime:
                dt_str = parse_filename_datetime(filename)
                if dt_str:
                    if dt_str in telemetry_by_dt:
                        ponto_gps = telemetry_by_dt[dt_str]
                        t_mapped = ponto_gps['start_time']

            if not ponto_gps:
                # Mapeamento sequencial baseado no intervalo temporal
                t = args.start + idx * args.photo_interval
                ponto_gps = obter_telemetria_mais_proxima(t, telemetry)
                t_mapped = t

            if not ponto_gps:
                print(f"   [AVISO] Telemetria não encontrada para a foto '{filename}'. Pulando.")
                continue

            frame_count += 1
            out_img_path = os.path.join(args.out, f"photo_{frame_count:04d}_{filename}")
            shutil.copy2(photo_path, out_img_path)

            lat = ponto_gps['latitude']
            lon = ponto_gps['longitude']
            alt = ponto_gps['altitude']

            injetor_exif.injetar_gps(out_img_path, lat, lon, alt)
            
            dt_log = f" ({ponto_gps.get('datetime')})" if ponto_gps.get('datetime') else ""
            print(f"Foto {frame_count} ({filename}) processada - Lat: {lat:.6f}, Lon: {lon:.6f}, Alt: {alt:.1f}m (t={t_mapped:.2f}s){dt_log}")

    print("\n--- PIPELINE CONCLUÍDO COM SUCESSO ---")
    print(f"Total de fotos georreferenciadas geradas: {frame_count}")
    print(f"Local dos arquivos: {os.path.abspath(args.out)}")

if __name__ == "__main__":
    main()
