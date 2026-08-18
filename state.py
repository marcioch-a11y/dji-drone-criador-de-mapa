import threading
import subprocess
import time

# Sincronização e processos globais
process_lock = threading.Lock()
active_process = None
log_messages = []

# Sistema de Fila de Projetos (Batch)
queue_lock = threading.Lock()
project_queue = []
current_job = None


def add_log(msg):
    """Adiciona uma mensagem aos logs em tempo real com timestamp se necessário."""
    global log_messages
    log_messages.append(str(msg))


def clear_logs(header="--- NOVO PROCESSO INICIADO ---"):
    """Limpa o buffer de logs e adiciona um cabeçalho."""
    global log_messages
    log_messages.clear()
    if header:
        log_messages.append(header)
