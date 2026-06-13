import os
import piexif

def dms_para_decimal(deg, minutes, sec, ref):
    """
    Converte coordenadas no formato Graus, Minutos e Segundos EXIF (frações racionais)
    para graus decimais normais.
    """
    d = deg[0] / deg[1]
    m = minutes[0] / minutes[1]
    s = sec[0] / sec[1]
    
    decimal = d + m / 60.0 + s / 3600.0
    if ref in [b'S', b'W', 'S', 'W']:
        decimal = -decimal
    return decimal

def verificar_imagens(dir_path):
    print("=== INICIANDO AUDITORIA DE METADADOS EXIF ===")
    if not os.path.isdir(dir_path):
        print(f"Erro: Diretório '{dir_path}' não encontrado.")
        return

    files = sorted([f for f in os.listdir(dir_path) if f.lower().endswith('.jpg')])
    
    if not files:
        print("Erro: Nenhuma imagem JPEG encontrada no diretório de saída.")
        return

    for filename in files:
        filepath = os.path.join(dir_path, filename)
        print(f"\nVerificando arquivo: {filename}")
        try:
            exif_dict = piexif.load(filepath)
            gps = exif_dict.get("GPS", {})
            
            if not gps:
                print("   -> [ERRO] Nenhuma tag de GPS encontrada no EXIF!")
                continue
                
            lat_ref = gps.get(piexif.GPSIFD.GPSLatitudeRef)
            lat_val = gps.get(piexif.GPSIFD.GPSLatitude)
            lon_ref = gps.get(piexif.GPSIFD.GPSLongitudeRef)
            lon_val = gps.get(piexif.GPSIFD.GPSLongitude)
            alt_ref = gps.get(piexif.GPSIFD.GPSAltitudeRef)
            alt_val = gps.get(piexif.GPSIFD.GPSAltitude)
            
            if lat_val and lat_ref and lon_val and lon_ref:
                lat_decimal = dms_para_decimal(lat_val[0], lat_val[1], lat_val[2], lat_ref)
                lon_decimal = dms_para_decimal(lon_val[0], lon_val[1], lon_val[2], lon_ref)
                
                altitude = alt_val[0] / alt_val[1] if alt_val else 0.0
                if alt_ref == 1:
                    altitude = -altitude
                    
                print(f"   -> [OK] Dados GPS validados:")
                print(f"      Latitude:  {lat_decimal:.6f} ({lat_ref.decode('ascii')})")
                print(f"      Longitude: {lon_decimal:.6f} ({lon_ref.decode('ascii')})")
                print(f"      Altitude:  {altitude:.2f}m")
            else:
                print("   -> [ERRO] Estrutura de tags GPS EXIF incompleta!")
                
        except Exception as e:
            print(f"   -> [ERRO] Falha crítica ao ler metadados EXIF: {e}")

if __name__ == "__main__":
    verificar_imagens("output_images")
