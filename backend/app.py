"""
Backend do Halter Inteligente

1. Conecta ao haltere por Bluetooth (BLE) e repassa cada leitura ao jogo em tempo real (Server-Sent Events)
2. Guarda pacientes, sessões e repetições em SQLite e calcula as métricas consolidadas
3. Serve o frontend

Rodar:  python backend/app.py   →  jogo em http://localhost:5000/game/
Login padrão: fisio / halter123 (troque com as variáveis HALTER_USUARIO e HALTER_SENHA)
"""
import asyncio
import os
import queue
import sqlite3
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path

from bleak import BleakClient, BleakScanner
from flask import Flask, Response, abort, g, jsonify, redirect, request, send_from_directory, session

RAIZ = Path(__file__).resolve().parent.parent
FRONT = RAIZ / 'frontend'
BANCO = RAIZ / 'data' / 'halter.db'
BRUTOS = RAIZ / 'data' / 'raw'                       # CSV com todas as leituras de cada sessão
TX_UUID = '6e400003-b5a3-f393-e0a9-e50e24dcca9e'     # característica de envio do firmware (UART Nordic)
USUARIO = os.getenv('HALTER_USUARIO', 'fisio')
SENHA = os.getenv('HALTER_SENHA', 'halter123')

app = Flask(__name__, static_folder=str(FRONT), static_url_path='')
app.secret_key = os.getenv('HALTER_SEGREDO', 'troque-esta-chave')


# =====================================================================
# 1. HALTERE → BACKEND → JOGO (tempo real)
# =====================================================================
ouvintes = []              # uma fila por navegador conectado em /api/stream
estado_haltere = 'iniciando'
gravacao = None            # arquivo CSV aberto enquanto há uma sessão em andamento


def publicar(evento, dado):
    """Envia um evento para todos os navegadores conectados."""
    for fila in list(ouvintes):
        try:
            fila.put_nowait(f'event: {evento}\ndata: {dado}\n\n')
        except queue.Full:
            pass           # navegador lento: descarta em vez de travar o Bluetooth


def receber(_, dados: bytearray):
    """Chamada a cada pacote BLE do ESP32: 't_ms,theta,ax,ay,az'."""
    linha = dados.decode(errors='ignore').strip()
    if not linha or linha.startswith('#'):
        return
    publicar('dado', linha)
    arquivo = gravacao
    if arquivo:
        try:
            arquivo.write(linha + '\n')
        except ValueError:
            pass           # a sessão acabou de ser encerrada e o arquivo foi fechado


def avisar(texto):
    global estado_haltere
    estado_haltere = texto
    publicar('status', texto)


async def laco_bluetooth():
    """Procura o haltere, assina as notificações e reconecta sozinho se cair."""
    while True:
        try:
            avisar('procurando haltere…')
            disp = await BleakScanner.find_device_by_filter(lambda d, _: (d.name or '').startswith('Haltere'))
            if disp is None:
                continue
            async with BleakClient(disp) as cliente:
                await cliente.start_notify(TX_UUID, receber)
                avisar(f'conectado ({disp.name})')
                while cliente.is_connected:
                    await asyncio.sleep(0.5)
            avisar('desconectado')
        except Exception as erro:
            avisar(f'erro no Bluetooth: {erro}')
            await asyncio.sleep(5)


@app.get('/api/stream')
def stream():
    """Fluxo contínuo (SSE) com as leituras do haltere para o jogo."""
    fila = queue.Queue(maxsize=200)
    ouvintes.append(fila)

    def gerar():
        try:
            yield f'event: status\ndata: {estado_haltere}\n\n'
            while True:
                try:
                    yield fila.get(timeout=15)
                except queue.Empty:
                    yield ': ping\n\n'                 # mantém a conexão aberta
        finally:
            ouvintes.remove(fila)

    return Response(gerar(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache'})


# =====================================================================
# 2. BANCO DE DADOS
# =====================================================================
ESQUEMA = '''
CREATE TABLE IF NOT EXISTS patients (
  id INTEGER PRIMARY KEY, nome TEXT NOT NULL, nascimento TEXT, observacoes TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id), inicio TEXT, fim TEXT,
  exercicio TEXT, cadencia_alvo REAL, amp_min REAL, amp_max REAL, duracao_min REAL,          -- configuração
  duracao_s REAL, total_reps INTEGER, amp_media REAL, vel_media REAL,                         -- resultados
  aderencia_cadencia REAL, obstaculos TEXT, pontuacao INTEGER);
CREATE TABLE IF NOT EXISTS reps (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id), n INTEGER, t_ms INTEGER,
  amplitude REAL, vel_pico REAL, duracao_s REAL, no_ritmo INTEGER, cadencia REAL);
'''


def db():
    if 'db' not in g:
        g.db = sqlite3.connect(BANCO)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def fechar_db(_):
    conexao = g.pop('db', None)
    if conexao:
        conexao.close()


def consulta(sql, *args):
    return [dict(linha) for linha in db().execute(sql, args)]


def sessao_aberta(sid):
    linha = db().execute('SELECT * FROM sessions WHERE id = ?', (sid,)).fetchone()
    if linha is None:
        abort(404)
    if linha['fim']:
        abort(409)        # sessão já encerrada
    return linha


@app.errorhandler(ValueError)
@app.errorhandler(TypeError)
@app.errorhandler(KeyError)
def dado_invalido(erro):
    return jsonify(erro=f'dado inválido: {erro}'), 400


# =====================================================================
# 3. AUTENTICAÇÃO
# =====================================================================
def login_obrigatorio(rota):
    @wraps(rota)
    def protegida(*args, **kwargs):
        if not session.get('usuario'):
            abort(401)
        return rota(*args, **kwargs)
    return protegida


@app.post('/api/login')
def login():
    dados = request.get_json(force=True)
    if dados.get('usuario') != USUARIO or dados.get('senha') != SENHA:
        abort(401)
    session['usuario'] = USUARIO
    return jsonify(ok=True)


@app.post('/api/logout')
def logout():
    session.clear()
    return jsonify(ok=True)


# =====================================================================
# 4. API — PACIENTES, SESSÕES E REPETIÇÕES
# =====================================================================
@app.get('/api/patients')
@login_obrigatorio
def listar_pacientes():
    return jsonify(consulta('SELECT * FROM patients ORDER BY nome'))


@app.post('/api/patients')
@login_obrigatorio
def criar_paciente():
    d = request.get_json(force=True)
    nome = str(d['nome']).strip()
    if not nome:
        raise ValueError('nome vazio')
    cur = db().execute('INSERT INTO patients (nome, nascimento, observacoes) VALUES (?, ?, ?)',
                       (nome, d.get('nascimento'), d.get('observacoes')))
    db().commit()
    return jsonify(id=cur.lastrowid), 201


@app.get('/api/patients/<int:pid>/sessions')
@login_obrigatorio
def historico(pid):
    return jsonify(consulta('SELECT * FROM sessions WHERE patient_id = ? ORDER BY inicio', pid))


@app.post('/api/sessions')
@login_obrigatorio
def criar_sessao():
    """Recebe a configuração do profissional, cria a sessão e devolve o link do jogo já configurado."""
    global gravacao
    d = request.get_json(force=True)
    cfg = dict(patient_id=int(d['patient_id']), exercicio=str(d.get('exercicio', 'flexão de cotovelo')),
               cadencia_alvo=float(d['cadencia_alvo']), amp_min=float(d.get('amp_min', 0)),
               amp_max=float(d.get('amp_max', 60)), duracao_min=float(d.get('duracao_min', 3)))
    if not 4 <= cfg['cadencia_alvo'] <= 30:
        raise ValueError('cadência fora de 4–30 rep/min')
    cur = db().execute('INSERT INTO sessions (patient_id, inicio, exercicio, cadencia_alvo, amp_min, amp_max, duracao_min) '
                       'VALUES (:patient_id, :inicio, :exercicio, :cadencia_alvo, :amp_min, :amp_max, :duracao_min)',
                       {**cfg, 'inicio': datetime.now().isoformat(timespec='seconds')})
    db().commit()
    sid = cur.lastrowid
    if gravacao:
        gravacao.close()
    gravacao = open(BRUTOS / f'sessao_{sid}.csv', 'w', buffering=1)
    gravacao.write('t_ms,theta,ax,ay,az\n')
    jogo = (f"/game/?sessao={sid}&cadencia={cfg['cadencia_alvo']:g}&min={cfg['amp_min']:g}"
            f"&max={cfg['amp_max']:g}&duracao={cfg['duracao_min']:g}")
    return jsonify(id=sid, jogo=jogo), 201


@app.post('/api/sessions/<int:sid>/reps')
@login_obrigatorio
def salvar_repeticao(sid):
    """Valida e armazena cada repetição enviada pelo jogo."""
    sessao_aberta(sid)
    d = request.get_json(force=True)
    opcional = lambda k, tipo: None if d.get(k) is None else tipo(d[k])
    rep = dict(session_id=sid, n=int(d['n']), t_ms=int(d['t_ms']), amplitude=float(d['amplitude']),
               vel_pico=float(d['vel_pico']), duracao_s=opcional('duracao_s', float),
               no_ritmo=opcional('no_ritmo', bool), cadencia=float(d['cadencia']))
    if not 0 <= rep['amplitude'] <= 360 or rep['vel_pico'] < 0:
        raise ValueError('amplitude ou velocidade fora da faixa')
    db().execute('INSERT INTO reps (session_id, n, t_ms, amplitude, vel_pico, duracao_s, no_ritmo, cadencia) '
                 'VALUES (:session_id, :n, :t_ms, :amplitude, :vel_pico, :duracao_s, :no_ritmo, :cadencia)', rep)
    db().commit()
    return jsonify(ok=True), 201


@app.post('/api/sessions/<int:sid>/end')
@login_obrigatorio
def encerrar_sessao(sid):
    """Calcula as métricas consolidadas a partir das repetições salvas e fecha a sessão."""
    global gravacao
    sessao_aberta(sid)
    d = request.get_json(silent=True) or {}
    m = db().execute('SELECT COUNT(*) AS total_reps, ROUND(AVG(amplitude), 1) AS amp_media, '
                     'ROUND(AVG(vel_pico), 1) AS vel_media, ROUND(AVG(no_ritmo), 3) AS aderencia_cadencia '
                     'FROM reps WHERE session_id = ?', (sid,)).fetchone()
    resultado = {**dict(m), 'id': sid, 'fim': datetime.now().isoformat(timespec='seconds'),
                 'duracao_s': d.get('duracao_s'), 'obstaculos': d.get('obstaculos'), 'pontuacao': d.get('pontuacao')}
    db().execute('UPDATE sessions SET fim = :fim, duracao_s = :duracao_s, total_reps = :total_reps, '
                 'amp_media = :amp_media, vel_media = :vel_media, aderencia_cadencia = :aderencia_cadencia, '
                 'obstaculos = :obstaculos, pontuacao = :pontuacao WHERE id = :id', resultado)
    db().commit()
    if gravacao:
        gravacao.close()
        gravacao = None
    return jsonify(resultado)


@app.get('/api/sessions/<int:sid>')
@login_obrigatorio
def detalhe_sessao(sid):
    sessao = consulta('SELECT * FROM sessions WHERE id = ?', sid)
    if not sessao:
        abort(404)
    return jsonify(**sessao[0], reps=consulta('SELECT * FROM reps WHERE session_id = ? ORDER BY n', sid))


# =====================================================================
# 5. FRONTEND
# =====================================================================
@app.get('/')
def inicio():
    if (FRONT / 'index.html').exists():
        return send_from_directory(FRONT, 'index.html')
    return redirect('/game/')


@app.get('/game/')
def jogo():
    return send_from_directory(FRONT / 'game', 'index.html')


# =====================================================================
if __name__ == '__main__':
    BRUTOS.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(BANCO) as conexao:
        conexao.executescript(ESQUEMA)
    threading.Thread(target=lambda: asyncio.run(laco_bluetooth()), daemon=True).start()
    app.run(port=5000, threaded=True)
