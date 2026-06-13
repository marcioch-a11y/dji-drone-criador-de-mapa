import re

def parse_time(time_str):
    """
    Converte uma string de tempo no formato HH:MM:SS,mmm (ou HH:MM:SS.mmm) para segundos em float.
    """
    time_str = time_str.replace(',', '.')
    parts = time_str.split(':')
    if len(parts) == 3:
        h, m, s = parts
        return float(h) * 3600.0 + float(m) * 60.0 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return float(m) * 60.0 + float(s)
    return float(time_str)

def parse_srt(srt_path):
    """
    Lê o arquivo SRT da DJI e extrai a matriz de tempo/coordenadas.
    Retorna uma lista de dicionários contendo:
    'index', 'start_time', 'end_time', 'latitude', 'longitude', 'altitude'
    """
    with open(srt_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    # Divide o arquivo por blocos de legenda (linhas em branco)
    blocks = re.split(r'\n\s*\n', content.strip())
    telemetry_data = []

    # Regex para formatos rotulados comuns (ex: [latitude : -23.123] [longtitude : -46.456] ou lat: -23.123)
    # Aceita 'longitude' e a grafia incorreta 'longtitude' típica de alguns firmwares da DJI.
    lat_regex = re.compile(r'(?:latitude|lat)\s*[:=]\s*(-?\d+\.\d+)', re.IGNORECASE)
    lon_regex = re.compile(r'(?:longtitude|longitude|lon|lng)\s*[:=]\s*(-?\d+\.\d+)', re.IGNORECASE)
    alt_regex = re.compile(r'(?:altitude|alt)\s*[:=]\s*(-?\d+\.\d+)', re.IGNORECASE)

    # Regex para formato abreviado de função (ex: GPS(-46.456, -23.123, 15.4) ou GPS(latitude, longitude, altitude))
    gps_regex = re.compile(r'gps\s*\(\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)(?:\s*,\s*(-?\d+\.\d+))?\s*\)', re.IGNORECASE)

    for block in blocks:
        lines = [line.strip() for line in block.split('\n') if line.strip()]
        if len(lines) < 3:
            continue

        # Linha 1: Índice
        try:
            index = int(lines[0])
        except ValueError:
            continue

        # Linha 2: Intervalo de tempo (ex: 00:00:01,000 --> 00:00:02,000)
        time_line = lines[1]
        if '-->' not in time_line:
            continue

        times = time_line.split('-->')
        if len(times) != 2:
            continue

        try:
            start_time = parse_time(times[0].strip())
            end_time = parse_time(times[1].strip())
        except ValueError:
            continue

        # Linhas restantes: Texto com a telemetria
        text = ' '.join(lines[2:])

        lat = None
        lon = None
        alt = 0.0

        # Tenta extrair usando padrões rotulados
        lat_match = lat_regex.search(text)
        lon_match = lon_regex.search(text)
        alt_match = alt_regex.search(text)

        if lat_match and lon_match:
            lat = float(lat_match.group(1))
            lon = float(lon_match.group(1))
            if alt_match:
                alt = float(alt_match.group(1))
        else:
            # Caso não encontre rótulos estruturados, busca o formato de função GPS(...)
            gps_match = gps_regex.search(text)
            if gps_match:
                val1 = float(gps_match.group(1))
                val2 = float(gps_match.group(2))
                val3 = float(gps_match.group(3)) if gps_match.group(3) is not None else 0.0

                # Lógica heurística para determinar qual é latitude e qual é longitude
                # Longitude varia de -180 a 180. Latitude de -90 a 90.
                # Se o primeiro valor for maior do que 90 (em módulo), indica longitude.
                if abs(val1) > 90.0:
                    lon = val1
                    lat = val2
                else:
                    # Caso contrário, assume formato padrão GPS(latitude, longitude, altitude)
                    lat = val1
                    lon = val2
                alt = val3

        # Tenta extrair a data/hora do bloco de telemetria (ex: 2026-06-13 13:58:10)
        dt_match = re.search(r'(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})', text)
        dt_str = dt_match.group(0) if dt_match else None

        if lat is not None and lon is not None:
            telemetry_data.append({
                'index': index,
                'start_time': start_time,
                'end_time': end_time,
                'latitude': lat,
                'longitude': lon,
                'altitude': alt,
                'datetime': dt_str
            })

    return telemetry_data
