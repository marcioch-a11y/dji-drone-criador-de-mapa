import piexif
from fractions import Fraction

def change_to_rational(number):
    """
    Converte um número (int, float) em uma fração racional (numerador, denominador) 
    no formato exigido pelo EXIF.
    """
    # Usar a representação em string previne imprecisões de ponto flutuante do Python
    f = Fraction(str(number)).limit_denominator(1000000)
    return (f.numerator, f.denominator)

def to_dms(value, directions):
    """
    Converte uma coordenada decimal para Graus, Minutos e Segundos (DMS).
    
    Args:
        value (float): Valor decimal da coordenada.
        directions (list): Lista com [direção_negativa, direção_positiva], ex: ['S', 'N'].
        
    Returns:
        tuple: (graus, minutos, segundos, caractere_ref)
    """
    direction = directions[1] if value >= 0 else directions[0]
    abs_value = abs(value)
    
    deg = int(abs_value)
    min_float = (abs_value - deg) * 60.0
    minutes = int(min_float)
    sec = round((min_float - minutes) * 60.0, 5)
    
    # Tratamento de arredondamento de borda
    if sec >= 60.0:
        sec = 0.0
        minutes += 1
        if minutes >= 60:
            minutes = 0
            deg += 1
            
    return deg, minutes, sec, direction

def injetar_gps(img_path, lat, lon, alt):
    """
    Injeta dados de GPS (Latitude, Longitude e Altitude) no cabeçalho EXIF de um arquivo JPEG.
    
    Args:
        img_path (str): Caminho absoluto da imagem JPEG.
        lat (float): Latitude em graus decimais.
        lon (float): Longitude em graus decimais.
        alt (float): Altitude em metros.
    """
    # Converte Latitude e Longitude para DMS
    lat_deg, lat_min, lat_sec, lat_ref = to_dms(lat, ['S', 'N'])
    lon_deg, lon_min, lon_sec, lon_ref = to_dms(lon, ['W', 'E'])
    
    # Monta a estrutura racional EXIF
    exif_lat = (
        change_to_rational(lat_deg),
        change_to_rational(lat_min),
        change_to_rational(lat_sec)
    )
    exif_lon = (
        change_to_rational(lon_deg),
        change_to_rational(lon_min),
        change_to_rational(lon_sec)
    )
    
    # Determina a referência de altitude (0 = Acima do nível do mar, 1 = Abaixo)
    alt_ref = 0 if alt >= 0 else 1
    exif_alt = change_to_rational(abs(alt))
    
    # Cria o dicionário do bloco de GPS
    gps_ifd = {
        piexif.GPSIFD.GPSVersionID: (2, 2, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: lat_ref.encode('ascii'),
        piexif.GPSIFD.GPSLatitude: exif_lat,
        piexif.GPSIFD.GPSLongitudeRef: lon_ref.encode('ascii'),
        piexif.GPSIFD.GPSLongitude: exif_lon,
        piexif.GPSIFD.GPSAltitudeRef: alt_ref,
        piexif.GPSIFD.GPSAltitude: exif_alt
    }
    
    # Carrega metadados EXIF existentes na imagem (se houver)
    try:
        exif_dict = piexif.load(img_path)
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        
    # Atualiza o dicionário com os dados GPS
    exif_dict["GPS"] = gps_ifd
    
    # Serializa e injeta de volta na imagem
    exif_bytes = piexif.dump(exif_dict)
    piexif.insert(exif_bytes, img_path)

def tem_gps(img_path):
    """
    Verifica se a imagem já possui coordenadas GPS válidas inseridas no EXIF.
    """
    try:
        exif_dict = piexif.load(img_path)
        gps = exif_dict.get("GPS", {})
        return (piexif.GPSIFD.GPSLatitude in gps) and (piexif.GPSIFD.GPSLongitude in gps)
    except Exception:
        return False

