import os
import string
from ctypes import windll
from flask import Blueprint, request, jsonify

explorer_bp = Blueprint('explorer_bp', __name__)

@explorer_bp.route('/api/list-directory', methods=['POST'])
def list_directory():
    """
    Lista arquivos e subpastas de um diretório para o explorador de arquivos web.
    """
    data = request.json or {}
    path = data.get('path')
    show_files = data.get('show_files', True)
    file_type = data.get('file_type', 'all')

    if not path or not os.path.exists(path):
        path = os.path.expanduser('~')

    path = os.path.abspath(path)

    # Detecta drives disponíveis no Windows
    drives = []
    bitmask = windll.kernel32.GetLogicalDrives()
    for letter in string.ascii_uppercase:
        if bitmask & 1:
            drives.append(f"{letter}:\\")
        bitmask >>= 1

    folders = []
    files = []
    parent_path = os.path.dirname(path) if path != os.path.abspath(os.sep) else None

    try:
        for item in os.listdir(path):
            full_path = os.path.join(path, item)
            # Ignora pastas/arquivos ocultos/sistema
            if item.startswith('.') or item.startswith('$'):
                continue
            if os.path.isdir(full_path):
                folders.append(item)
            elif os.path.isfile(full_path) and show_files:
                ext = item.lower().split('.')[-1] if '.' in item else ''
                if file_type == 'video' and ext not in ['mp4', 'avi', 'mkv', 'mov']:
                    continue
                if file_type == 'srt' and ext != 'srt':
                    continue
                files.append(item)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    folders.sort(key=str.lower)
    files.sort(key=str.lower)

    shortcuts = {
        'Início (Home)': os.path.expanduser('~'),
        'Área de Trabalho (Desktop)': os.path.join(os.path.expanduser('~'), 'Desktop'),
        'Documentos': os.path.join(os.path.expanduser('~'), 'Documents'),
        'Downloads': os.path.join(os.path.expanduser('~'), 'Downloads')
    }
    shortcuts = {k: v for k, v in shortcuts.items() if os.path.exists(v)}

    return jsonify({
        'current_path': path,
        'parent_path': parent_path if parent_path != path else None,
        'drives': drives,
        'folders': folders,
        'files': files,
        'shortcuts': shortcuts
    })
