import os
import re
import io
import math
import base64
import zipfile
import tempfile
import cv2
import numpy as np
from PIL import Image
from flask import Blueprint, request, jsonify
from kmz_helper import (
    generate_visual_adjustment_kmz,
    extract_kmz_info,
    parse_kml_coordinates,
)

kmz_bp = Blueprint('kmz_bp', __name__)


def _extract_kmz_image_and_coords(kmz_path, target_max_dim=4096):
    """Auxiliar para extrair a imagem de alta resolução e as 4 coordenadas [SW, SE, NE, NW] de um KMZ."""
    with zipfile.ZipFile(kmz_path, 'r') as z:
        names = z.namelist()
        root_kml = '0/0/0.kml' if '0/0/0.kml' in names else [n for n in names if n.endswith('.kml')][0]
        txt = z.read(root_kml).decode('utf-8', errors='ignore')
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
            req_lvl = max(0, int(round(math.log2(max(256, target_max_dim) / 256.0))))
            available_lvls = [lvl for lvl in levels if lvl <= req_lvl]
            target_lvl = max(available_lvls) if available_lvls else max(levels)
            
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


@kmz_bp.route('/api/generate-adjust-kmz', methods=['POST'])
def generate_adjust_kmz():
    """
    Gera o KMZ de ajuste visual leve com alta taxa de contraste em falsa cor.
    """
    data = request.json or {}
    kmz_path = data.get('kmz_path', '').strip()
    print(f"[Generate KMZ] Path received: {kmz_path}")

    if not kmz_path or not os.path.exists(kmz_path):
        return jsonify({'status': 'error', 'message': 'Arquivo KMZ original não encontrado.'}), 400

    if not os.path.isfile(kmz_path) or not kmz_path.lower().endswith('.kmz'):
        return jsonify({'status': 'error', 'message': 'Por favor, selecione um arquivo com a extensão .kmz.'}), 400

    try:
        output_kmz = generate_visual_adjustment_kmz(kmz_path)
        return jsonify({
            'status': 'success',
            'message': 'KMZ de ajuste visual criado com sucesso!',
            'output_path': output_kmz
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 400


@kmz_bp.route('/api/apply-visual-adjust', methods=['POST'])
def apply_visual_adjust():
    """
    Aplica a rotação, escala e translação feitas visualmente no Google Earth ao KMZ original.
    """
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

        with zipfile.ZipFile(adjusted_kmz, 'r') as z:
            names = z.namelist()
            kmls = [n for n in names if n.lower().endswith('.kml')]
            if not kmls:
                return jsonify({'status': 'error', 'message': 'Nenhum KML encontrado no KMZ ajustado.'}), 400
            txt_adj = z.read(kmls[0]).decode('utf-8', errors='ignore')

            n_m = re.search(r'<north>([^<]+)</north>', txt_adj)
            s_m = re.search(r'<south>([^<]+)</south>', txt_adj)
            e_m = re.search(r'<east>([^<]+)</east>', txt_adj)
            w_m = re.search(r'<west>([^<]+)</west>', txt_adj)
            r_m = re.search(r'<rotation>([^<]+)</rotation>', txt_adj)

            if not (n_m and s_m and e_m and w_m):
                return jsonify({'status': 'error', 'message': 'O KMZ ajustado não contém as tags <LatLonBox> esperadas.'}), 400

            n1 = float(n_m.group(1))
            s1 = float(s_m.group(1))
            e1 = float(e_m.group(1))
            w1 = float(w_m.group(1))
            rot_deg_1 = float(r_m.group(1)) if r_m else 0.0

            c_lat_1 = (n1 + s1) / 2.0
            c_lon_1 = (e1 + w1) / 2.0
            w_lon_1 = (e1 - w1)
            h_lat_1 = (n1 - s1)
            rot_1_rad = math.radians(rot_deg_1)

        delta_rot = rot_1_rad - rot_0_rad
        cos_drot = math.cos(delta_rot)
        sin_drot = math.sin(delta_rot)
        scale_x = w_lon_1 / w_lon_0 if w_lon_0 > 0 else 1.0
        scale_y = h_lat_1 / h_lat_0 if h_lat_0 > 0 else 1.0

        def transform_point(lon, lat):
            dx = (lon - c_lon_0) * cos0
            dy = (lat - c_lat_0)
            dx_scaled = dx * scale_x
            dy_scaled = dy * scale_y
            dx_rot = dx_scaled * cos_drot - dy_scaled * sin_drot
            dy_rot = dx_scaled * sin_drot + dy_scaled * cos_drot
            lon_new = c_lon_1 + (dx_rot / cos0)
            lat_new = c_lat_1 + dy_rot
            return lon_new, lat_new

        def transform_kml(kml_text):
            def repl_quad(match):
                pts = match.group(1).strip().split()
                new_pts = []
                for pt in pts:
                    parts = pt.split(',')
                    if len(parts) >= 2:
                        lon = float(parts[0])
                        lat = float(parts[1])
                        alt = parts[2] if len(parts) > 2 else "0"
                        n_lon, n_lat = transform_point(lon, lat)
                        new_pts.append(f"{n_lon:.8f},{n_lat:.8f},{alt}")
                return f"<coordinates>\n            {' '.join(new_pts)}\n          </coordinates>"

            pattern_quad = re.compile(r'<coordinates>\s*([^<]+)\s*</coordinates>')
            kml_text = pattern_quad.sub(repl_quad, kml_text)

            def repl_box(match):
                tag = match.group(1) # LatLonBox ou LatLonAltBox
                body = match.group(2)
                nm = re.search(r'<north>([^<]+)</north>', body)
                sm = re.search(r'<south>([^<]+)</south>', body)
                em = re.search(r'<east>([^<]+)</east>', body)
                wm = re.search(r'<west>([^<]+)</west>', body)
                if nm and sm and em and wm:
                    n_val, s_val = float(nm.group(1)), float(sm.group(1))
                    e_val, w_val = float(em.group(1)), float(wm.group(1))
                    _, n_new = transform_point(c_lon_0, n_val)
                    _, s_new = transform_point(c_lon_0, s_val)
                    e_new, _ = transform_point(e_val, c_lat_0)
                    w_new, _ = transform_point(w_val, c_lat_0)
                    new_body = body
                    new_body = re.sub(r'<north>[^<]+</north>', f'<north>{n_new:.8f}</north>', new_body)
                    new_body = re.sub(r'<south>[^<]+</south>', f'<south>{s_new:.8f}</south>', new_body)
                    new_body = re.sub(r'<east>[^<]+</east>', f'<east>{e_new:.8f}</east>', new_body)
                    new_body = re.sub(r'<west>[^<]+</west>', f'<west>{w_new:.8f}</west>', new_body)
                    if rot_deg_1 != 0.0:
                        if '<rotation>' in new_body:
                            new_body = re.sub(r'<rotation>[^<]+</rotation>', f'<rotation>{rot_deg_1:.6f}</rotation>', new_body)
                        else:
                            new_body += f'\n        <rotation>{rot_deg_1:.6f}</rotation>'
                    return f"<{tag}>{new_body}</{tag}>"
                return match.group(0)

            pattern_box = re.compile(r'<(LatLonBox|LatLonAltBox)>(.*?)</\1>', re.DOTALL)
            kml_text = pattern_box.sub(repl_box, kml_text)

            return kml_text

        dir_orig = os.path.dirname(original_kmz)
        base_orig = os.path.basename(original_kmz)
        name_orig, ext_orig = os.path.splitext(base_orig)
        output_kmz_path = os.path.join(dir_orig, f"{name_orig}_ajustado_visual{ext_orig}")

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

                        content_updated = transform_kml(content)
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(content_updated)
                        kml_count += 1
                    elif file.lower() == 'metadata.json':
                        file_path = os.path.join(root, file)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                meta_data = json.load(f)
                            if 'georeference' in meta_data:
                                geo = meta_data['georeference']
                                if 'center' in geo:
                                    c_lon_new, c_lat_new = transform_point(geo['center']['longitude'], geo['center']['latitude'])
                                    geo['center']['latitude'] = c_lat_new
                                    geo['center']['longitude'] = c_lon_new
                                if 'bounds' in geo:
                                    _, n_new = transform_point(c_lon_0, geo['bounds']['north'])
                                    _, s_new = transform_point(c_lon_0, geo['bounds']['south'])
                                    e_new, _ = transform_point(geo['bounds']['east'], c_lat_0)
                                    w_new, _ = transform_point(geo['bounds']['west'], c_lat_0)
                                    geo['bounds']['north'] = n_new
                                    geo['bounds']['south'] = s_new
                                    geo['bounds']['east'] = e_new
                                    geo['bounds']['west'] = w_new
                                if 'corners' in geo:
                                    for c_name, c_pt in geo['corners'].items():
                                        lon_c_new, lat_c_new = transform_point(c_pt[0], c_pt[1])
                                        geo['corners'][c_name] = [lon_c_new, lat_c_new]
                            with open(file_path, 'w', encoding='utf-8') as f:
                                json.dump(meta_data, f, indent=2)
                        except Exception as ex_m:
                            print(f"[KMZ ADJUST] Erro atualizando metadata.json: {ex_m}")

            with zipfile.ZipFile(output_kmz_path, 'w', zipfile.ZIP_DEFLATED) as zip_out:
                for root, dirs, files in os.walk(temp_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, temp_dir)
                        zip_out.write(file_path, arcname)

        d_lat_m = (c_lat_1 - c_lat_0) * 111139.0
        d_lon_m = (c_lon_1 - c_lon_0) * cos0 * 111139.0
        dist_m = math.sqrt(d_lat_m**2 + d_lon_m**2)

        return jsonify({
            'status': 'success',
            'message': f'Ajuste visual aplicado com sucesso a {kml_count} arquivos KML! Deslocamento: {dist_m:.2f}m (Lat: {d_lat_m:+.2f}m, Lon: {d_lon_m:+.2f}m), Rotação: {math.degrees(delta_rot):+.2f}°',
            'output_path': output_kmz_path
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Erro ao aplicar ajuste visual: {str(e)}'}), 500


@kmz_bp.route('/api/adjust-kmz', methods=['POST'])
def adjust_kmz():
    """
    Ajusta as coordenadas geográficas de um KMZ (SuperOverlay) deslocando-o em metros.
    """
    data = request.json or {}
    kmz_path = data.get('kmz_path')
    lat_shift_m = float(data.get('lat_shift_m', 0.0))
    lon_shift_m = float(data.get('lon_shift_m', 0.0))

    if not kmz_path or not os.path.exists(kmz_path):
        return jsonify({'status': 'error', 'message': 'Arquivo KMZ original não encontrado.'}), 400

    dir_name = os.path.dirname(kmz_path)
    base_name = os.path.basename(kmz_path)
    name_part, ext_part = os.path.splitext(base_name)
    output_kmz_path = os.path.join(dir_name, f"{name_part}_ajustado{ext_part}")

    try:
        avg_lat = -20.0
        with zipfile.ZipFile(kmz_path, 'r') as z:
            for name in z.namelist():
                if name.lower().endswith('.kml'):
                    content = z.read(name).decode('utf-8', errors='ignore')
                    lat_match = re.search(r'<latitude>([^<]+)</latitude>', content)
                    if lat_match:
                        avg_lat = float(lat_match.group(1))
                        break
                    north_match = re.search(r'<north>([^<]+)</north>', content)
                    south_match = re.search(r'<south>([^<]+)</south>', content)
                    if north_match and south_match:
                        avg_lat = (float(north_match.group(1)) + float(south_match.group(1))) / 2.0
                        break

        lat_rad = math.radians(avg_lat)
        lat_len = 111132.95 - 559.82 * math.cos(2 * lat_rad) + 1.175 * math.cos(4 * lat_rad)
        lon_len = 111412.84 * math.cos(lat_rad) - 93.5 * math.cos(3 * lat_rad)

        lat_shift_deg = lat_shift_m / lat_len
        lon_shift_deg = lon_shift_m / lon_len

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


@kmz_bp.route('/api/merge-kmz', methods=['POST'])
def merge_kmz():
    """
    Mescla 2 ou mais arquivos KMZ com fusão contínua raster ou em camadas individuais.
    """
    data = request.json or {}
    kmz_paths = data.get('kmz_paths', [])
    output_name = data.get('output_name', '').strip()
    merge_mode = data.get('mode', 'seamless')

    if not kmz_paths or len(kmz_paths) < 2:
        return jsonify({'status': 'error', 'message': 'Selecione pelo menos 2 arquivos KMZ para unir.'}), 400

    valid_paths = [p.strip() for p in kmz_paths if p and os.path.exists(p.strip()) and p.strip().lower().endswith('.kmz')]
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
            global_w = max_dim if aspect >= 1.0 else max(256, int(max_dim * aspect))
            global_h = max(256, int(max_dim / aspect)) if aspect >= 1.0 else max_dim

            def lonlat_to_pixel(lon, lat):
                x = (lon - min_lon) / (max_lon - min_lon) * (global_w - 1)
                y = (max_lat - lat) / (max_lat - min_lat) * (global_h - 1)
                return x, y

            warped_maps = []
            dist_maps = []

            for arr, (sw, se, ne, nw) in maps_data:
                h, w = arr.shape[:2]
                src_pts = np.float32([[0, h - 1], [w - 1, h - 1], [w - 1, 0], [0, 0]])
                dst_pts = np.float32([lonlat_to_pixel(sw[0], sw[1]), lonlat_to_pixel(se[0], se[1]), lonlat_to_pixel(ne[0], ne[1]), lonlat_to_pixel(nw[0], nw[1])])
                M = cv2.getPerspectiveTransform(src_pts, dst_pts)
                warped_rgba = cv2.warpPerspective(arr, M, (global_w, global_h), flags=cv2.INTER_LINEAR)
                alpha = warped_rgba[:, :, 3]
                valid_mask = (alpha > 10).astype(np.uint8)
                dist = cv2.distanceTransform(valid_mask, cv2.DIST_L2, 5)
                warped_maps.append(warped_rgba)
                dist_maps.append(dist)

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
                        fused_rgba[both, c] = np.clip(warped_maps[0][both, c].astype(np.float32) * w0 + warped_maps[1][both, c].astype(np.float32) * w1, 0, 255).astype(np.uint8)
                    fused_rgba[both, 3] = np.maximum(warped_maps[0][both, 3], warped_maps[1][both, 3])
            else:
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
            with tempfile.TemporaryDirectory() as temp_dir:
                folders_kml = []
                for idx, kmz_file in enumerate(valid_paths, start=1):
                    subfolder = f"map_{idx}"
                    sub_dir = os.path.join(temp_dir, subfolder)
                    os.makedirs(sub_dir, exist_ok=True)
                    with zipfile.ZipFile(kmz_file, 'r') as z:
                        z.extractall(sub_dir)
                    base_n = os.path.splitext(os.path.basename(kmz_file))[0]
                    entry_kml = "doc.kml" if os.path.exists(os.path.join(sub_dir, "doc.kml")) else [f for f in os.listdir(sub_dir) if f.lower().endswith('.kml')][0]
                    folders_kml.append(f"""    <Folder>
      <name>{base_n}</name>
      <open>1</open>
      <NetworkLink>
        <name>{base_n}</name>
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


@kmz_bp.route('/api/convert-to-lightweight-kmz', methods=['POST'])
def convert_to_lightweight_kmz():
    data = request.json or {}
    kmz_path = data.get('kmz_path')
    quality = data.get('quality', 'large')

    if not kmz_path or not os.path.exists(kmz_path) or not os.path.isfile(kmz_path):
        return jsonify({'status': 'error', 'message': 'Arquivo KMZ original não encontrado.'}), 400

    quality_resolutions = {'small': (1024, '1024px'), 'medium': (2048, '2048px'), 'large': (4096, '4096px'), 'ultra': (8192, '8192px')}
    max_dim, qual_label = quality_resolutions.get(quality, (4096, '4096px'))

    dir_name = os.path.dirname(kmz_path)
    base_name = os.path.basename(kmz_path)
    name_part, ext_part = os.path.splitext(base_name)
    output_leve_kmz = os.path.join(dir_name, f"{name_part}_leve_{quality}{ext_part}")

    try:
        arr, corners = _extract_kmz_image_and_coords(kmz_path, target_max_dim=max_dim)
        sw, se, ne, nw = corners
        img = Image.fromarray(arr)
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='PNG', optimize=True)
        coords_str = f"{sw[0]:.8f},{sw[1]:.8f},0 {se[0]:.8f},{se[1]:.8f},0 {ne[0]:.8f},{ne[1]:.8f},0 {nw[0]:.8f},{nw[1]:.8f},0"

        doc_kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
  <Document>
    <name>{name_part} (Leve - {qual_label})</name>
    <GroundOverlay>
      <name>{name_part}_leve_{quality}</name>
      <description>KMZ Leve Otimizado de Imagem Única ({qual_label})</description>
      <drawOrder>999</drawOrder>
      <Icon>
        <href>preview.png</href>
      </Icon>
      <gx:LatLonQuad>
        <coordinates>
          {coords_str}
        </coordinates>
      </gx:LatLonQuad>
    </GroundOverlay>
  </Document>
</kml>"""

        with zipfile.ZipFile(output_leve_kmz, 'w', zipfile.ZIP_DEFLATED) as zip_out:
            zip_out.writestr("doc.kml", doc_kml_content)
            zip_out.writestr("preview.png", img_byte_arr.getvalue())

        size_mb = os.path.getsize(output_leve_kmz) / (1024 * 1024)
        return jsonify({
            'status': 'success',
            'message': f'KMZ leve ({qual_label}, {size_mb:.1f} MB) gerado com sucesso!',
            'output_path': output_leve_kmz
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Erro ao gerar o KMZ leve: {str(e)}'}), 500


@kmz_bp.route('/api/photo-to-kmz', methods=['POST'])
def photo_to_kmz():
    import piexif

    data = request.json or {}
    img_path = data.get('img_path')

    if not img_path or not os.path.exists(img_path) or not os.path.isfile(img_path):
        return jsonify({'status': 'error', 'message': 'Arquivo de imagem não encontrado.'}), 400

    def parse_rational(rat):
        if not rat or len(rat) < 2 or rat[1] == 0:
            return 0.0
        return float(rat[0]) / float(rat[1])

    try:
        exif_dict = piexif.load(img_path)
        gps = exif_dict.get("GPS", {})
        if not gps or piexif.GPSIFD.GPSLatitude not in gps or piexif.GPSIFD.GPSLongitude not in gps:
            return jsonify({'status': 'error', 'message': 'Esta foto não possui coordenadas GPS válidas nos metadados EXIF.'}), 400

        lat_ref = gps[piexif.GPSIFD.GPSLatitudeRef].decode('ascii')
        lat_dms = gps[piexif.GPSIFD.GPSLatitude]
        lat = parse_rational(lat_dms[0]) + parse_rational(lat_dms[1])/60.0 + parse_rational(lat_dms[2])/3600.0
        if lat_ref == 'S': lat = -lat
        
        lon_ref = gps[piexif.GPSIFD.GPSLongitudeRef].decode('ascii')
        lon_dms = gps[piexif.GPSIFD.GPSLongitude]
        lon = parse_rational(lon_dms[0]) + parse_rational(lon_dms[1])/60.0 + parse_rational(lon_dms[2])/3600.0
        if lon_ref == 'W': lon = -lon
        
        alt = 40.0
        if piexif.GPSIFD.GPSAltitude in gps:
            alt_val = parse_rational(gps[piexif.GPSIFD.GPSAltitude])
            if alt_val > 0: alt = alt_val
                
        fov_h_rad = math.radians(85.0)
        fov_v_rad = math.radians(65.0)
        width_m = 2.0 * alt * math.tan(fov_h_rad / 2.0)
        height_m = 2.0 * alt * math.tan(fov_v_rad / 2.0)
        
        lat_rad = math.radians(lat)
        lat_len = 111132.95 - 559.82 * math.cos(2 * lat_rad) + 1.175 * math.cos(4 * lat_rad)
        lon_len = 111412.84 * math.cos(lat_rad) - 93.5 * math.cos(3 * lat_rad)
        
        lat_delta = (height_m / 2.0) / lat_len
        lon_delta = (width_m / 2.0) / lon_len
        
        north, south, east, west = lat + lat_delta, lat - lat_delta, lon + lon_delta, lon - lon_delta
        dir_name = os.path.dirname(img_path)
        base_name = os.path.basename(img_path)
        name_part, _ = os.path.splitext(base_name)
        output_kmz = os.path.join(dir_name, f"{name_part}.kmz")

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


@kmz_bp.route('/api/get-kmz-info', methods=['POST'])
def get_kmz_info():
    data = request.json or {}
    kmz_path = data.get('kmz_path')

    if not kmz_path or not os.path.exists(kmz_path) or not os.path.isfile(kmz_path):
        return jsonify({'status': 'error', 'message': 'Arquivo KMZ não encontrado.'}), 400

    try:
        size_mb = os.path.getsize(kmz_path) / (1024 * 1024)
        arr, corners = _extract_kmz_image_and_coords(kmz_path, target_max_dim=1024)
        sw, se, ne, nw = corners

        north = max(sw[1], se[1], ne[1], nw[1])
        south = min(sw[1], se[1], ne[1], nw[1])
        east = max(sw[0], se[0], ne[0], nw[0])
        west = min(sw[0], se[0], ne[0], nw[0])

        img = Image.fromarray(arr)
        img_thumb = img.copy()
        img_thumb.thumbnail((512, 512), Image.Resampling.LANCZOS)
        
        buf = io.BytesIO()
        img_thumb.save(buf, format='PNG', optimize=True)
        b64_str = base64.b64encode(buf.getvalue()).decode('utf-8')

        return jsonify({
            'status': 'success',
            'name': os.path.basename(kmz_path),
            'size_mb': round(size_mb, 1),
            'dimensions': {'width': img.width, 'height': img.height},
            'bounds': {'north': north, 'south': south, 'east': east, 'west': west, 'center_lat': (north + south) / 2.0, 'center_lon': (east + west) / 2.0},
            'quad': {'sw': sw, 'se': se, 'ne': ne, 'nw': nw},
            'preview_url': f"data:image/png;base64,{b64_str}"
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Erro ao inspecionar KMZ: {str(e)}'}), 500


@kmz_bp.route('/api/crop-kmz', methods=['POST'])
def crop_kmz():
    data = request.json or {}
    kmz_path = data.get('kmz_path')
    crop_mode = data.get('crop_mode', 'percent')
    quality = data.get('quality', 'large')
    custom_name = data.get('custom_name', '').strip()

    if not kmz_path or not os.path.exists(kmz_path) or not os.path.isfile(kmz_path):
        return jsonify({'status': 'error', 'message': 'Arquivo KMZ de origem não encontrado.'}), 400

    quality_map = {'small': 1024, 'medium': 2048, 'large': 4096, 'ultra': 8192}
    max_dim = quality_map.get(quality, 4096)

    try:
        dir_name = os.path.dirname(kmz_path)
        base_name = os.path.basename(kmz_path)
        name_part, _ = os.path.splitext(base_name)
        if not custom_name: custom_name = f"{name_part}_recorte"

        arr, corners = _extract_kmz_image_and_coords(kmz_path, target_max_dim=max_dim)
        sw0, se0, ne0, nw0 = corners
        img_full = Image.fromarray(arr)
        img_w, img_h = img_full.size

        def get_geo_at_uv(u, v):
            u, v = max(0.0, min(1.0, u)), max(0.0, min(1.0, v))
            lon = (1.0 - u) * (1.0 - v) * nw0[0] + u * (1.0 - v) * ne0[0] + (1.0 - u) * v * sw0[0] + u * v * se0[0]
            lat = (1.0 - u) * (1.0 - v) * nw0[1] + u * (1.0 - v) * ne0[1] + (1.0 - u) * v * sw0[1] + u * v * se0[1]
            return lon, lat

        def solve_bilinear_uv_from_geo(lon, lat):
            u, v = 0.5, 0.5
            p00, p10, p01, p11 = sw0, se0, nw0, ne0
            for _ in range(25):
                fx = (1-u)*(1-v)*p00[0] + u*(1-v)*p10[0] + (1-u)*v*p01[0] + u*v*p11[0] - lon
                fy = (1-u)*(1-v)*p00[1] + u*(1-v)*p10[1] + (1-u)*v*p01[1] + u*v*p11[1] - lat
                if abs(fx) < 1e-11 and abs(fy) < 1e-11: break
                dfx_du = -(1-v)*p00[0] + (1-v)*p10[0] - v*p01[0] + v*p11[0]
                dfx_dv = -(1-u)*p00[0] - u*p10[0] + (1-u)*p01[0] + u*p11[0]
                dfy_du = -(1-v)*p00[1] + (1-v)*p10[1] - v*p01[1] + v*p11[1]
                dfy_dv = -(1-u)*p00[1] - u*p10[1] + (1-u)*p01[1] + u*p11[1]
                det = dfx_du * dfy_dv - dfx_dv * dfy_du
                if abs(det) < 1e-18: break
                u -= (dfy_dv * fx - dfx_dv * fy) / det
                v -= (-dfy_du * fx + dfx_du * fy) / det
            return max(0.0, min(1.0, u)), 1.0 - max(0.0, min(1.0, v))

        def save_sub_kmz(u_min, v_min, u_max, v_max, output_filepath, label=""):
            x1 = max(0, min(img_w - 2, int(round(u_min * img_w))))
            y1 = max(0, min(img_h - 2, int(round(v_min * img_h))))
            x2 = max(x1 + 2, min(img_w, int(round(u_max * img_w))))
            y2 = max(y1 + 2, min(img_h, int(round(v_max * img_h))))

            sub_img = img_full.crop((x1, y1, x2, y2))
            if max(sub_img.width, sub_img.height) > max_dim:
                sub_img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

            c_nw = get_geo_at_uv(u_min, v_min)
            c_ne = get_geo_at_uv(u_max, v_min)
            c_se = get_geo_at_uv(u_max, v_max)
            c_sw = get_geo_at_uv(u_min, v_max)

            coords_str = f"{c_sw[0]:.8f},{c_sw[1]:.8f},0 {c_se[0]:.8f},{c_se[1]:.8f},0 {c_ne[0]:.8f},{c_ne[1]:.8f},0 {c_nw[0]:.8f},{c_nw[1]:.8f},0"
            sub_name = os.path.splitext(os.path.basename(output_filepath))[0]

            doc_kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
  <Document>
    <name>{sub_name}</name>
    <GroundOverlay>
      <name>{sub_name}</name>
      <description>Recorte Otimizado de KMZ {label}</description>
      <drawOrder>999</drawOrder>
      <Icon>
        <href>preview.png</href>
      </Icon>
      <gx:LatLonQuad>
        <coordinates>
          {coords_str}
        </coordinates>
      </gx:LatLonQuad>
    </GroundOverlay>
  </Document>
</kml>"""

            img_bytes = io.BytesIO()
            sub_img.save(img_bytes, format='PNG', optimize=True)
            with zipfile.ZipFile(output_filepath, 'w', zipfile.ZIP_DEFLATED) as zout:
                zout.writestr("doc.kml", doc_kml)
                zout.writestr("preview.png", img_bytes.getvalue())

            size_mb = os.path.getsize(output_filepath) / (1024 * 1024)
            return {'path': output_filepath, 'name': os.path.basename(output_filepath), 'size_mb': round(size_mb, 2), 'dimensions': f"{sub_img.width}x{sub_img.height}px"}

        generated_files = []
        if crop_mode == 'percent':
            pct_top = float(data.get('pct_top', 0.0)) / 100.0
            pct_bottom = float(data.get('pct_bottom', 0.0)) / 100.0
            pct_left = float(data.get('pct_left', 0.0)) / 100.0
            pct_right = float(data.get('pct_right', 0.0)) / 100.0
            u_min = max(0.0, min(0.95, pct_left))
            u_max = max(u_min + 0.05, min(1.0, 1.0 - pct_right))
            v_min = max(0.0, min(0.95, pct_top))
            v_max = max(v_min + 0.05, min(1.0, 1.0 - pct_bottom))
            out_file = os.path.join(dir_name, f"{custom_name}.kmz")
            generated_files.append(save_sub_kmz(u_min, v_min, u_max, v_max, out_file, "(Recorte por Margem)"))
        elif crop_mode == 'bounds':
            u1, v1 = solve_bilinear_uv_from_geo(float(data.get('west')), float(data.get('north')))
            u2, v2 = solve_bilinear_uv_from_geo(float(data.get('east')), float(data.get('south')))
            out_file = os.path.join(dir_name, f"{custom_name}.kmz")
            generated_files.append(save_sub_kmz(min(u1, u2), min(v1, v2), max(u1, u2), max(v1, v2), out_file, "(Recorte por Coordenadas)"))
        elif crop_mode == 'grid':
            grid_type = data.get('grid_type', '2x2')
            rows, cols = (1, 2) if grid_type == '1x2' else (2, 1) if grid_type == '2x1' else (3, 3) if grid_type == '3x3' else (2, 2)
            overlap, part_num = 0.05, 1
            for r in range(rows):
                for c in range(cols):
                    u_min = max(0.0, (c / cols) - (overlap if c > 0 else 0.0))
                    u_max = min(1.0, ((c + 1) / cols) + (overlap if c < cols - 1 else 0.0))
                    v_min = max(0.0, (r / rows) - (overlap if r > 0 else 0.0))
                    v_max = min(1.0, ((r + 1) / rows) + (overlap if r < rows - 1 else 0.0))
                    out_file = os.path.join(dir_name, f"{custom_name}_parte_{part_num:02d}_R{r+1}C{c+1}.kmz")
                    generated_files.append(save_sub_kmz(u_min, v_min, u_max, v_max, out_file, f"(Parte {part_num} de {rows*cols})"))
                    part_num += 1

        return jsonify({'status': 'success', 'message': f'{len(generated_files)} arquivo(s) KMZ recortado(s) com sucesso!', 'files': generated_files})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Erro ao recortar KMZ: {str(e)}'}), 500
