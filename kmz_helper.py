import os
import re
import io
import math
import json
import zipfile
from datetime import datetime
import numpy as np
from PIL import Image, ImageEnhance, ImageDraw


def parse_kml_coordinates(kml_content):
    """
    Extrai coordenadas geográficas de qualquer arquivo KML (LatLonQuad ou LatLonBox),
    independentemente de namespaces (gx:, kml:, etc.).
    Retorna: (north, south, east, west, rotation_deg, center_lat, center_lon, coords_quad)
    """
    # 1. Tenta extrair gx:LatLonQuad ou LatLonQuad
    quad_match = re.search(r'<(?:\w+:)?(?:LatLonQuad|gx:LatLonQuad)>[\s\S]*?<(?:\w+:)?coordinates>\s*([^<]+)\s*</(?:\w+:)?coordinates>', kml_content, re.IGNORECASE)
    if not quad_match:
        quad_match = re.search(r'<(?:\w+:)?coordinates>\s*([^<]+)\s*</(?:\w+:)?coordinates>', kml_content, re.IGNORECASE)

    if quad_match:
        pts = quad_match.group(1).strip().split()
        coords = []
        for pt in pts:
            parts = pt.split(',')
            if len(parts) >= 2:
                try:
                    coords.append((float(parts[0]), float(parts[1])))
                except:
                    pass
        
        if len(coords) >= 4:
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
            
            width_deg = math.sqrt(((se_lon - sw_lon) * cos_lat) ** 2 + (se_lat - sw_lat) ** 2) / (cos_lat if cos_lat > 0 else 1.0)
            height_deg = math.sqrt(((nw_lon - sw_lon) * cos_lat) ** 2 + (nw_lat - sw_lat) ** 2)
            
            north_coord = center_lat + height_deg / 2.0
            south_coord = center_lat - height_deg / 2.0
            east_coord = center_lon + width_deg / 2.0
            west_coord = center_lon - width_deg / 2.0

            return north_coord, south_coord, east_coord, west_coord, rotation_deg, center_lat, center_lon, coords

    # 2. Tenta extrair LatLonBox padrão (north, south, east, west)
    n_m = re.search(r'<(?:\w+:)?north>\s*([-]?\d+\.?\d*)\s*</(?:\w+:)?north>', kml_content, re.I)
    s_m = re.search(r'<(?:\w+:)?south>\s*([-]?\d+\.?\d*)\s*</(?:\w+:)?south>', kml_content, re.I)
    e_m = re.search(r'<(?:\w+:)?east>\s*([-]?\d+\.?\d*)\s*</(?:\w+:)?east>', kml_content, re.I)
    w_m = re.search(r'<(?:\w+:)?west>\s*([-]?\d+\.?\d*)\s*</(?:\w+:)?west>', kml_content, re.I)
    r_m = re.search(r'<(?:\w+:)?rotation>\s*([-]?\d+\.?\d*)\s*</(?:\w+:)?rotation>', kml_content, re.I)

    if n_m and s_m and e_m and w_m:
        north = float(n_m.group(1))
        south = float(s_m.group(1))
        east = float(e_m.group(1))
        west = float(w_m.group(1))
        rot = float(r_m.group(1)) if r_m else 0.0
        c_lat = (north + south) / 2.0
        c_lon = (east + west) / 2.0
        coords = [(west, south), (east, south), (east, north), (west, north)]
        return north, south, east, west, rot, c_lat, c_lon, coords

    return None, None, None, None, 0.0, None, None, None


def extract_kmz_info(kmz_path):
    """
    Lê qualquer arquivo KMZ e extrai:
    - Imagem base PIL RGBA (resolução otimizada)
    - Coordenadas geográficas limites e rotação
    - Metadados estruturados
    """
    if not os.path.exists(kmz_path):
        raise FileNotFoundError(f"Arquivo KMZ não encontrado: {kmz_path}")

    dir_name = os.path.dirname(kmz_path)

    with zipfile.ZipFile(kmz_path, 'r') as z:
        names = z.namelist()

        # 1. Busca arquivo KML
        kmls = [n for n in names if n.lower().endswith('.kml')]
        if not kmls:
            raise ValueError("Nenhum arquivo KML encontrado dentro do arquivo KMZ.")

        # Prioriza 0/0/0.kml -> doc.kml -> qualquer kml
        root_kml = None
        for k in kmls:
            if k.lower().endswith('0/0/0.kml'):
                root_kml = k
                break
        if not root_kml:
            for k in kmls:
                if k.lower().endswith('doc.kml'):
                    root_kml = k
                    break
        if not root_kml:
            root_kml = kmls[0]

        kml_content = z.read(root_kml).decode('utf-8', errors='ignore')
        north, south, east, west, rotation_deg, center_lat, center_lon, coords = parse_kml_coordinates(kml_content)

        # Fallback 2: metadata.json embutido
        meta_files = [n for n in names if n.lower().endswith('metadata.json')]
        if meta_files and (north is None or center_lat is None):
            try:
                mdata = json.loads(z.read(meta_files[0]).decode('utf-8'))
                if 'georeference' in mdata and 'bounds' in mdata['georeference']:
                    b = mdata['georeference']['bounds']
                    north = float(b['north'])
                    south = float(b['south'])
                    east = float(b['east'])
                    west = float(b['west'])
                    center_lat = (north + south) / 2.0
                    center_lon = (east + west) / 2.0
                    rotation_deg = 0.0
            except:
                pass

        if north is None or south is None or east is None or west is None:
            raise ValueError("Não foi possível ler as coordenadas geográficas do KMZ.")

        # 2. Extração da Imagem
        base_img = None

        # Método A: GeoTIFF na mesma pasta se existir
        tif_names = ["odm_orthophoto_leve.tif", "odm_orthophoto.tif"]
        for tif_name in tif_names:
            tif_p = os.path.join(dir_name, tif_name)
            if os.path.exists(tif_p):
                try:
                    with Image.open(tif_p) as img:
                        img.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
                        base_img = img.convert('RGBA')
                        break
                except:
                    pass

        # Método B: Mosaico de Tiles em pirâmide
        if base_img is None:
            try:
                png_tiles = [n for n in names if n.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')) and '/' in n]
                levels = sorted(list(set([int(n.split('/')[0]) for n in png_tiles if n.split('/')[0].isdigit()])))
                if levels:
                    target_lvl = min(max(levels), 4)
                    sample_candidates = [n for n in png_tiles if n.startswith(f'{target_lvl}/')]
                    if sample_candidates:
                        sample_img = Image.open(io.BytesIO(z.read(sample_candidates[0])))
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
            except:
                pass

        # Método C: Imagem única dentro do ZIP
        if base_img is None:
            img_candidates = [n for n in names if n.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff')) and not n.startswith('__')]
            def img_priority(n):
                nl = n.lower()
                if 'preview' in nl: return 999999999
                if 'doc' in nl: return 888888888
                if '0/0/0' in nl: return 777777777
                return z.getinfo(n).file_size

            img_candidates.sort(key=img_priority, reverse=True)
            for c in img_candidates:
                try:
                    data = z.read(c)
                    if len(data) > 0:
                        base_img = Image.open(io.BytesIO(data)).convert('RGBA')
                        break
                except:
                    pass

        if base_img is None:
            raise ValueError("Nenhuma imagem válida pôde ser extraída do arquivo KMZ.")

        return {
            'img': base_img,
            'north': north,
            'south': south,
            'east': east,
            'west': west,
            'rotation': rotation_deg,
            'center_lat': center_lat,
            'center_lon': center_lon,
            'coords': coords
        }


def generate_visual_adjustment_kmz(kmz_path, output_path=None):
    """
    Gera um KMZ otimizado e de alto contraste em falsa-cor (Magenta/Amarelo)
    para alinhamento visual preciso e rápido no Google Earth Pro.
    """
    info = extract_kmz_info(kmz_path)
    base_img = info['img']
    north = info['north']
    south = info['south']
    east = info['east']
    west = info['west']
    rotation_deg = info['rotation']

    # Aplica efeito de alto contraste e borda vibrante
    arr = np.array(base_img, dtype=np.float32)
    alpha_mask = arr[:, :, 3] > 10 if arr.shape[2] == 4 else np.ones(arr.shape[:2], dtype=bool)

    # Realce Magenta / Violeta vivo para contraste absoluto sobre imagens de satélite
    arr[:, :, 0] = np.clip(arr[:, :, 0] * 1.5 + 50, 0, 255)
    arr[:, :, 1] = np.clip(arr[:, :, 1] * 0.15, 0, 255)
    arr[:, :, 2] = np.clip(arr[:, :, 2] * 1.3 + 40, 0, 255)
    if arr.shape[2] == 4:
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
    png_bytes = img_byte_arr.getvalue()

    if not output_path:
        dir_name = os.path.dirname(kmz_path)
        output_path = os.path.join(dir_name, "odm_orthophoto_ajuste_visual.kmz")

    doc_kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
  <Document>
    <name>Ajuste Visual - Google Earth</name>
    <GroundOverlay>
      <name>Mova-me no Google Earth (Ajustador)</name>
      <description>Mova esta imagem no Google Earth Pro (Botao Direito -&gt; Propriedades) e depois salve como KMZ para aplicar o ajuste no mapa completo.</description>
      <drawOrder>999</drawOrder>
      <Icon>
        <href>preview.png</href>
      </Icon>
      <LatLonBox>
        <north>{north:.8f}</north>
        <south>{south:.8f}</south>
        <east>{east:.8f}</east>
        <west>{west:.8f}</west>
        <rotation>{rotation_deg:.8f}</rotation>
      </LatLonBox>
    </GroundOverlay>
  </Document>
</kml>"""

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zout:
        zout.writestr("doc.kml", doc_kml)
        zout.writestr("preview.png", png_bytes)

    return output_path
