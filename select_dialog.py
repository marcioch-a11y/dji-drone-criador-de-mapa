import sys
import tkinter as tk
from tkinter import filedialog

def main():
    if len(sys.argv) < 2:
        sys.exit(1)
        
    mode = sys.argv[1] # 'file' or 'folder'
    
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    
    if mode == 'file':
        file_type = sys.argv[2] if len(sys.argv) > 2 else 'all'
        file_types = [("Todos os arquivos", "*.*")]
        if file_type == 'video':
            file_types = [("Vídeo DJI MP4", "*.mp4"), ("Todos os arquivos", "*.*")]
        elif file_type == 'srt':
            file_types = [("Legenda de Telemetria SRT", "*.srt"), ("Todos os arquivos", "*.*")]
            
        path = filedialog.askopenfilename(
            title="Selecionar Arquivo",
            filetypes=file_types
        )
    else:
        path = filedialog.askdirectory(title="Selecionar Pasta")
        
    root.destroy()
    
    # Imprime o caminho selecionado para o stdout
    sys.stdout.write(path)

if __name__ == "__main__":
    main()
