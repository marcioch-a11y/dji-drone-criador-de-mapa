import os
import glob
import time
import sys
import argparse
from pyodm import Node

def main():
    parser = argparse.ArgumentParser(description="Processa imagens georreferenciadas no WebODM/NodeODM.")
    parser.add_argument("--photos", required=True, help="Diretório contendo as fotos georreferenciadas")
    parser.add_argument("--out", required=True, help="Diretório para salvar os resultados")
    parser.add_argument("--filter", default="*.jpg", help="Padrão de busca para filtrar arquivos (ex: DJI_20260610*.JPG). Padrão: *.jpg")
    parser.add_argument("--quality", default="medium", choices=["low", "medium", "high"], help="Qualidade do processamento (low, medium, high). Padrão: medium")
    parser.add_argument("--resolution", type=float, default=4.0, help="Resolução da ortofoto (cm/pixel). Padrão: 4.0")
    
    args = parser.parse_args()

    # Busca os arquivos no diretório
    pattern = args.filter
    files = glob.glob(os.path.join(args.photos, pattern))
    
    # Se não achar nada com o padrão específico, tenta alternar a extensão (maiúscula/minúscula)
    if not files:
        if pattern.endswith('.jpg'):
            files = glob.glob(os.path.join(args.photos, pattern[:-4] + '.JPG'))
        elif pattern.endswith('.JPG'):
            files = glob.glob(os.path.join(args.photos, pattern[:-4] + '.jpg'))
    
    # Se ainda assim não encontrar nada, busca qualquer arquivo de imagem na pasta (caso a pasta não tenha o prefixo frame_/photo_)
    if not files:
        extensions = ['*.jpg', '*.JPG', '*.jpeg', '*.JPEG', '*.png', '*.PNG']
        for ext in extensions:
            files.extend(glob.glob(os.path.join(args.photos, ext)))
    
    # Remove duplicatas mantendo a ordenação
    files = sorted(list(set(files)))
    
    print("=== INICIANDO PROCESSAMENTO NO WEBODM (NODEODM) ===")
    print(f"Diretório de entrada: {args.photos}")
    print(f"Filtro aplicado: {pattern}")
    print(f"Total de imagens encontradas: {len(files)}")
    
    if not files:
        print("Erro: Nenhuma imagem encontrada com o padrão especificado ou qualquer outro formato de imagem (.jpg, .png, .jpeg) na pasta informada.", file=sys.stderr)
        sys.exit(1)

        
    # Conexão com o NodeODM local
    print("\nConectando ao NodeODM na porta 3000...")
    try:
        node = Node('localhost', 3000)
    except Exception as e:
        print(f"Erro crítico ao conectar ao NodeODM: {e}", file=sys.stderr)
        sys.exit(1)

    # Configuração de opções do ODM
    options = {
        'dsm': True,
        'dtm': True,
        'orthophoto-resolution': args.resolution,
        'feature-quality': args.quality,
        'min-num-features': 4000,
        'orthophoto-kmz': True  # Gera nativamente o arquivo KMZ para Google Earth
    }
    
    print("\nEnviando imagens e iniciando processamento no WebODM...")
    try:
        task = node.create_task(files, options)
        print(f"   -> Tarefa criada com sucesso! ID: {task.info().uuid}")
        
        print("\nAcompanhando o progresso do processamento em tempo real:")
        last_status = None
        last_progress = -1
        
        while True:
            try:
                info = task.info()
                status = info.status
                progress = info.progress
                
                # Exibe atualizações de status ou progresso percentual
                if status != last_status or progress != last_progress:
                    status_name = status.name if hasattr(status, 'name') else str(status)
                    progress_str = f"{progress}%" if progress is not None else "Calculando..."
                    print(f"      [Status]: {status_name.upper()} | [Progresso]: {progress_str}")
                    last_status = status
                    last_progress = progress
                
                # Comparação de status de forma segura contra string/enum
                status_str = status.name.lower() if hasattr(status, 'name') else str(status).lower()
                if status_str in ['completed', 'failed', 'canceled']:
                     break
            except Exception as e:
                pass
                
            time.sleep(5)
            
        status_str = last_status.name.lower() if hasattr(last_status, 'name') else str(last_status).lower()
        if status_str == 'completed':
            print("\n=== PROCESSAMENTO CONCLUÍDO COM SUCESSO ===")
            print(f"Baixando arquivos geográficos finais para: {os.path.abspath(args.out)}")
            
            task.download_assets(args.out)
            
            # Localiza os caminhos dos principais arquivos
            kmz_path = os.path.join(args.out, 'odm_orthophoto', 'odm_orthophoto.kmz')
            tif_path = os.path.join(args.out, 'odm_orthophoto', 'odm_orthophoto.tif')
            
            print("\nArquivos baixados! Principais entregáveis disponíveis na pasta:")
            if os.path.isfile(tif_path):
                print(f"   - Ortofoto Geotiff:  {os.path.abspath(tif_path)}")
            if os.path.isfile(kmz_path):
                print(f"   - Ortofoto Google Earth: {os.path.abspath(kmz_path)}")
            print(f"   - DSM: {os.path.abspath(os.path.join(args.out, 'odm_dem', 'dsm.tif'))}")
        else:
            print(f"\n[ERRO] O processamento falhou. Status final do NodeODM: {status_str}", file=sys.stderr)
            sys.exit(1)
            
    except Exception as e:
        print(f"\n[ERRO] Ocorreu uma falha na execução da tarefa: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
