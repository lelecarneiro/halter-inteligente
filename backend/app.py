"""
Backend do Halter Inteligente

1. Conecta ao haltere por Bluetooth (BLE) e repassa cada leitura ao jogo em tempo real (Server-Sent Events)
2. Guarda pacientes, sessões e repetições em SQLite
3. Módulo de análise: aplica os métodos numéricos do memorial de cálculo às leituras de cada sessão
4. Serve a plataforma do profissional (/) e o jogo (/game/)

Rodar:  python backend/app.py   →  http://localhost:5000
Login padrão: fisio / halter123 (troque com as variáveis HALTER_USUARIO e HALTER_SENHA)
"""
import asyncio
import json
import os
import queue
import sqlite3
import threading
import warnings
from datetime import datetime
from functools import wraps
from pathlib import Path

import numpy as np
from bleak import BleakClient, BleakScanner
from flask import Flask, Response, abort, g, jsonify, make_response, request, send_from_directory, session

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
CREATE TABLE IF NOT EXISTS exercicios (
  id INTEGER PRIMARY KEY, nome TEXT NOT NULL UNIQUE COLLATE NOCASE, descricao TEXT, padrao INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS patients (
  id INTEGER PRIMARY KEY, nome TEXT NOT NULL, nascimento TEXT, observacoes TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id), inicio TEXT, fim TEXT,
  exercicio TEXT, exercicio_id INTEGER REFERENCES exercicios(id), braco TEXT, cadencia_alvo REAL, amp_min REAL, amp_max REAL,                 -- configuração
  duracao_min REAL, reps_alvo INTEGER,
  duracao_s REAL, total_reps INTEGER, amp_media REAL, vel_media REAL,                         -- resultados do jogo
  aderencia_cadencia REAL, obstaculos TEXT, pontuacao INTEGER,
  score_geral REAL, analise TEXT);                                                            -- módulo de análise (JSON)
CREATE TABLE IF NOT EXISTS reps (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id), n INTEGER, t_ms INTEGER,
  amplitude REAL, vel_pico REAL, duracao_s REAL, no_ritmo INTEGER, cadencia REAL);
'''

# Categorias de exercícios de membros superiores criadas automaticamente (o profissional pode criar outras)
EXERCICIOS_PADRAO = [
    ('Flexão de cotovelo', 'Rosca bíceps: dobrar o cotovelo trazendo o haltere em direção ao ombro'),
    ('Extensão de cotovelo', 'Tríceps: estender o cotovelo, com o braço elevado ou apoiado'),
    ('Elevação lateral', 'Abdução de ombro: elevar o braço estendido para o lado até a altura do ombro'),
    ('Elevação frontal', 'Flexão de ombro: elevar o braço estendido à frente até a altura do ombro'),
    ('Desenvolvimento de ombro', 'Elevar o haltere da altura do ombro até acima da cabeça'),
    ('Remada', 'Puxar o haltere em direção ao tronco, com o tronco inclinado ou apoiado'),
    ('Rotação externa de ombro', 'Cotovelo junto ao corpo a 90°: girar o antebraço para fora'),
    ('Flexão de punho', 'Antebraço apoiado, palma para cima: dobrar o punho'),
    ('Extensão de punho', 'Antebraço apoiado, palma para baixo: estender o punho'),
    ('Pronação e supinação', 'Girar o antebraço, virando a palma para cima e para baixo'),
]


def inicializar_banco():
    """Cria as tabelas, as categorias padrão e atualiza bancos de versões anteriores sem perder dados."""
    with sqlite3.connect(BANCO) as con:
        con.executescript(ESQUEMA)
        existentes = {coluna[1] for coluna in con.execute('PRAGMA table_info(sessions)')}
        for coluna, tipo in [('exercicio_id', 'INTEGER'), ('braco', 'TEXT'), ('reps_alvo', 'INTEGER'),
                             ('score_geral', 'REAL'), ('analise', 'TEXT')]:
            if coluna not in existentes:
                con.execute(f'ALTER TABLE sessions ADD COLUMN {coluna} {tipo}')
        con.executemany('INSERT OR IGNORE INTO exercicios (nome, descricao, padrao) VALUES (?, ?, 1)', EXERCICIOS_PADRAO)
        # Sessões antigas guardavam o exercício como texto livre: vira categoria
        con.execute("UPDATE sessions SET exercicio = 'Flexão de cotovelo' WHERE exercicio IS NULL OR TRIM(exercicio) = ''")
        con.execute('INSERT OR IGNORE INTO exercicios (nome) SELECT DISTINCT exercicio FROM sessions WHERE exercicio_id IS NULL')
        con.execute('UPDATE sessions SET exercicio_id = (SELECT id FROM exercicios e WHERE e.nome = sessions.exercicio) '
                    'WHERE exercicio_id IS NULL')
        con.execute("UPDATE sessions SET braco = 'afetado' WHERE braco IS NULL")


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


def falha(codigo, mensagem):
    """Interrompe a requisição com uma mensagem de erro legível pela plataforma."""
    abort(make_response(jsonify(erro=mensagem), codigo))


@app.errorhandler(ValueError)
@app.errorhandler(TypeError)
@app.errorhandler(KeyError)
def dado_invalido(erro):
    return jsonify(erro=f'dado inválido: {erro}'), 400


# =====================================================================
# 3. MÓDULO DE ANÁLISE (memorial de cálculo, seções 5.5 a 5.10)
# =====================================================================
K_MEDIA = 10                     # 5.7  janela da média móvel (amostras)
M_PONTOS = 101                   # 5.9  pontos da trajetória normalizada (τ = 0, 0,01, …, 1)
# 5.10 Erro a partir do qual a nota vale 0. Valores iniciais: calibrar com voluntários saudáveis.
ERRO_MAX = {'trajetoria': 15.0,     # RMSE da trajetória vs. ideal (°)
            'simetria': 15.0,       # RMSE braço afetado vs. saudável (°)
            'estabilidade': 0.05,   # desvio padrão da posição lateral Sx (m)
            'suavidade': 40.0}      # jerk médio = I_suavidade / duração (m/s³)
PESOS = {'trajetoria': 0.30, 'simetria': 0.30, 'estabilidade': 0.25, 'suavidade': 0.15}


def trapezio(y, t):
    """5.5 Integral acumulada pelo método dos trapézios: I(t_k) = I(t_k-1) + Δt/2 · [y(t_k-1) + y(t_k)]."""
    return np.concatenate([[0.0], np.cumsum(np.diff(t) / 2 * (y[1:] + y[:-1]))])


def media_movel(x, k=K_MEDIA):
    """5.7 Média das últimas K amostras: x_lenta(t_k) = (1/K) · Σ x(t_k-j), j = 0…K-1."""
    soma = np.concatenate([[0.0], np.cumsum(x)])
    n = np.arange(1, len(x) + 1)
    inicio = np.maximum(0, n - k)
    return (soma[n] - soma[inicio]) / (n - inicio)


def nota(erro, erro_max):
    """5.10 Nota_i = 100 · (1 − Erro_medido / Erro_max), limitada entre 0 e 100."""
    return float(np.clip(100 * (1 - erro / erro_max), 0, 100))


def arred(x, casas=3):
    """Número (ou lista) pronto para JSON: arredondado e sem NaN."""
    if isinstance(x, (list, np.ndarray)):
        return [arred(v, casas) for v in x]
    return None if x is None or not np.isfinite(x) else round(float(x), casas)


def ler_brutos(sid):
    """Leituras gravadas durante a sessão: colunas t_ms, θ, ax, ay, az (linhas incompletas são ignoradas)."""
    arquivo = BRUTOS / f'sessao_{sid}.csv'
    if not arquivo.exists():
        return None
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        d = np.genfromtxt(arquivo, delimiter=',', skip_header=1, invalid_raise=False, ndmin=2)
    d = d[np.isfinite(d).all(axis=1)] if d.size else d
    return d[np.argsort(d[:, 0], kind='stable')] if len(d) >= 10 else None


def analisar(sid, comparar_bracos=True):
    """Aplica os métodos numéricos às leituras brutas e às repetições da sessão."""
    sessao = consulta('SELECT * FROM sessions WHERE id = ?', sid)[0]
    reps = consulta('SELECT * FROM reps WHERE session_id = ? ORDER BY n', sid)
    dados = ler_brutos(sid)
    if dados is None or not reps:
        return None

    # Janelas de cada repetição (tempo do ESP32): do fim da anterior até o fim desta
    periodo_alvo = 60 / sessao['cadencia_alvo']
    fins = [r['t_ms'] / 1000 for r in reps]
    inicios = [fins[0] - (reps[0]['duracao_s'] or periodo_alvo)] + fins[:-1]
    t = dados[:, 0] / 1000
    no_exercicio = (t >= inicios[0]) & (t <= fins[-1])
    t, theta, ax, ay, az = (coluna[no_exercicio] for coluna in (t, *dados[:, 1:5].T))
    if len(t) < 10:
        return None
    duracao = t[-1] - t[0]

    # 5.4.3 Módulo da aceleração linear (sem gravidade)
    a_lin = np.hypot(ay, az)

    # 5.6 Jerk por diferenças finitas e índice de suavidade (trapézios de |J|)
    jerk = np.diff(a_lin) / np.diff(t)
    i_suavidade = trapezio(np.abs(jerk), t[1:])[-1]
    jerk_medio = i_suavidade / duracao

    # 5.7 Média móvel (K = 10) e resíduo → tremor; intensidade pelo RMS
    a_lenta = media_movel(a_lin)
    tremor = a_lin - a_lenta
    rms_tremor = np.sqrt(np.mean(tremor ** 2))

    # 5.5 Velocidade e posição por trapézios, repetição a repetição;
    # 5.9 tempo normalizado τ = t / T_total e interpolação linear da trajetória (θ)
    tau = np.linspace(0, 1, M_PONTOS)
    px = np.full_like(t, np.nan)
    curvas, vel_pico = [], []
    for ini, fim in zip(inicios, fins):
        j = (t >= ini) & (t <= fim)
        if j.sum() < 5:
            continue
        tr = t[j]
        frac = (tr - tr[0]) / (tr[-1] - tr[0])
        # remove o viés de cada eixo e força v = 0 no início e no fim da repetição (haltere parado),
        # senão o erro da integração dupla cresce sem limite
        v = np.column_stack([trapezio(a - a.mean(), tr) for a in (ax[j], ay[j], az[j])])
        v -= np.outer(frac, v[-1])
        p = np.column_stack([trapezio(v[:, k], tr) for k in range(3)])
        px[j] = p[:, 0]
        vel_pico.append(np.linalg.norm(v, axis=1).max())
        curvas.append(np.interp(tau, frac, theta[j]))
    if not curvas:
        return None

    # 5.8 Compensação postural: média, variância e desvio padrão amostral da posição lateral (eixo X)
    px_validos = px[np.isfinite(px)]
    mu_x, var_x = px_validos.mean(), px_validos.var(ddof=1)
    s_x = np.sqrt(var_x)

    # 5.9 Trajetória ideal com o mesmo recorte (cada repetição começa ao cruzar 30 % da amplitude, descendo)
    fase0 = 2 * np.pi - np.arccos(0.4)
    referencia = sessao['amp_min'] + (sessao['amp_max'] - sessao['amp_min']) * (0.5 - 0.5 * np.cos(fase0 + 2 * np.pi * tau))
    rmse_trajetoria = np.mean([np.sqrt(np.mean((c - referencia) ** 2)) for c in curvas])
    curva_media = np.mean(curvas, axis=0)

    # 5.9 Comparação entre braços: sessão mais recente do braço saudável do mesmo paciente
    rmse_simetria = atraso_apice = curva_saudavel = id_saudavel = None
    if comparar_bracos and sessao['braco'] == 'afetado':
        outra = consulta("SELECT id FROM sessions WHERE patient_id = ? AND exercicio_id IS ? AND braco = 'saudavel' "
                         'AND fim IS NOT NULL ORDER BY inicio DESC LIMIT 1', sessao['patient_id'], sessao['exercicio_id'])
        saudavel = analisar(outra[0]['id'], comparar_bracos=False) if outra else None
        if saudavel:
            id_saudavel = outra[0]['id']
            curva_saudavel = np.array(saudavel['curva_media'], dtype=float)
            rmse_simetria = np.sqrt(np.mean((curva_media - curva_saudavel) ** 2))
            sentido = np.sign(sessao['amp_max'] - sessao['amp_min']) or 1        # ápice = ponto mais alto
            atraso_apice = 100 * (tau[np.argmax(sentido * curva_media)] - tau[np.argmax(sentido * curva_saudavel)])

    # 5.10 Notas de 0 a 100 e score geral ponderado (pesos redistribuídos se faltar a simetria)
    erros = {'trajetoria': rmse_trajetoria, 'simetria': rmse_simetria, 'estabilidade': s_x, 'suavidade': jerk_medio}
    notas = {k: nota(e, ERRO_MAX[k]) for k, e in erros.items() if e is not None}
    score = sum(PESOS[k] * n for k, n in notas.items()) / sum(PESOS[k] for k in notas)

    passo = max(1, len(t) // 1500)                                              # séries reduzidas para os gráficos
    return {
        'metricas': {k: arred(v) for k, v in dict(
            duracao_s=duracao, amostras=len(t), i_suavidade=i_suavidade, jerk_medio=jerk_medio,
            rms_tremor=rms_tremor, mu_x=mu_x, var_x=var_x, s_x=s_x, vel_linear_pico=np.mean(vel_pico),
            rmse_trajetoria=rmse_trajetoria, rmse_simetria=rmse_simetria, atraso_apice_pct=atraso_apice).items()},
        'notas': {k: arred(v, 1) for k, v in notas.items()},
        'score_geral': arred(score, 1),
        'sessao_saudavel': id_saudavel,
        'curva_media': arred(curva_media, 2),
        'curva_referencia': arred(referencia, 2),
        'curva_saudavel': arred(curva_saudavel, 2) if curva_saudavel is not None else None,
        'serie': {k: arred(v[::passo]) for k, v in dict(
            t=t - t[0], theta=theta, theta_suave=media_movel(theta), a_lin=a_lin, a_lenta=a_lenta,
            tremor=tremor, jerk=np.concatenate([[0.0], jerk]), px=px).items()},
    }


# =====================================================================
# 4. AUTENTICAÇÃO
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


@app.get('/api/me')
def eu():
    return jsonify(usuario=session.get('usuario'))


# =====================================================================
# 5. API — PACIENTES, SESSÕES E REPETIÇÕES
# =====================================================================
@app.get('/api/patients')
@login_obrigatorio
def listar_pacientes():
    return jsonify(consulta('SELECT p.*, COUNT(s.id) AS sessoes, MAX(s.inicio) AS ultima_sessao FROM patients p '
                            'LEFT JOIN sessions s ON s.patient_id = p.id GROUP BY p.id ORDER BY p.nome'))


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


@app.get('/api/patients/<int:pid>')
@login_obrigatorio
def paciente(pid):
    linha = consulta('SELECT * FROM patients WHERE id = ?', pid)
    if not linha:
        abort(404)
    return jsonify(linha[0])


@app.put('/api/patients/<int:pid>')
@login_obrigatorio
def editar_paciente(pid):
    d = request.get_json(force=True)
    nome = str(d['nome']).strip()
    if not nome:
        raise ValueError('nome vazio')
    cur = db().execute('UPDATE patients SET nome = ?, nascimento = ?, observacoes = ? WHERE id = ?',
                       (nome, d.get('nascimento') or None, d.get('observacoes'), pid))
    db().commit()
    if cur.rowcount == 0:
        abort(404)
    return jsonify(ok=True)


@app.delete('/api/patients/<int:pid>')
@login_obrigatorio
def excluir_paciente(pid):
    """Exclui o paciente com todas as sessões, repetições e leituras brutas."""
    global gravacao
    if not consulta('SELECT id FROM patients WHERE id = ?', pid):
        abort(404)
    sessoes = [s['id'] for s in consulta('SELECT id FROM sessions WHERE patient_id = ?', pid)]
    for sid in sessoes:
        arquivo = BRUTOS / f'sessao_{sid}.csv'
        if gravacao and Path(gravacao.name) == arquivo:
            gravacao.close()
            gravacao = None
        arquivo.unlink(missing_ok=True)
    db().execute('DELETE FROM reps WHERE session_id IN (SELECT id FROM sessions WHERE patient_id = ?)', (pid,))
    db().execute('DELETE FROM sessions WHERE patient_id = ?', (pid,))
    db().execute('DELETE FROM patients WHERE id = ?', (pid,))
    db().commit()
    return jsonify(ok=True, sessoes_excluidas=len(sessoes))


# ---------- Categorias de exercícios ----------
@app.get('/api/exercicios')
@login_obrigatorio
def listar_exercicios():
    return jsonify(consulta('SELECT e.*, COUNT(s.id) AS sessoes FROM exercicios e LEFT JOIN sessions s '
                            'ON s.exercicio_id = e.id GROUP BY e.id ORDER BY e.padrao DESC, e.nome'))


@app.post('/api/exercicios')
@login_obrigatorio
def criar_exercicio():
    d = request.get_json(force=True)
    nome = ' '.join(str(d['nome']).split())
    if not 2 <= len(nome) <= 60:
        raise ValueError('o nome deve ter entre 2 e 60 caracteres')
    existente = consulta('SELECT nome FROM exercicios WHERE nome = ?', nome)
    if existente:
        falha(409, f'Já existe a categoria "{existente[0]["nome"]}".')
    cur = db().execute('INSERT INTO exercicios (nome, descricao) VALUES (?, ?)', (nome, d.get('descricao') or None))
    db().commit()
    return jsonify(id=cur.lastrowid, nome=nome), 201


@app.delete('/api/exercicios/<int:eid>')
@login_obrigatorio
def excluir_exercicio(eid):
    """Só exclui categorias criadas pelo profissional e que ainda não têm sessões."""
    e = consulta('SELECT e.padrao, COUNT(s.id) AS sessoes FROM exercicios e LEFT JOIN sessions s ON s.exercicio_id = e.id '
                 'WHERE e.id = ? GROUP BY e.id', eid)
    if not e:
        abort(404)
    if e[0]['padrao'] or e[0]['sessoes']:
        falha(409, 'Só é possível excluir categorias criadas por você e ainda sem sessões.')
    db().execute('DELETE FROM exercicios WHERE id = ?', (eid,))
    db().commit()
    return jsonify(ok=True)


SQL_SESSAO = 'SELECT s.*, e.nome AS exercicio_nome FROM sessions s LEFT JOIN exercicios e ON e.id = s.exercicio_id '


@app.get('/api/patients/<int:pid>/sessions')
@login_obrigatorio
def historico(pid):
    """Histórico do paciente; ?exercicio_id=<id> limita a um exercício."""
    filtro = request.args.get('exercicio_id', type=int)
    sessoes = consulta(SQL_SESSAO + 'WHERE s.patient_id = ? AND (? IS NULL OR s.exercicio_id = ?) ORDER BY s.inicio',
                       pid, filtro, filtro)
    for s in sessoes:
        s['analise'] = json.loads(s['analise']) if s['analise'] else None
    return jsonify(sessoes)


@app.post('/api/sessions')
@login_obrigatorio
def criar_sessao():
    """Recebe a configuração do profissional, cria a sessão e devolve o link do jogo já configurado."""
    global gravacao
    d = request.get_json(force=True)
    reps_alvo = d.get('reps_alvo')
    exercicio = consulta('SELECT id, nome FROM exercicios WHERE id = ?', int(d['exercicio_id']))
    if not exercicio:
        falha(404, 'Categoria de exercício não encontrada.')
    cfg = dict(patient_id=int(d['patient_id']), exercicio_id=exercicio[0]['id'], exercicio=exercicio[0]['nome'],
               braco=str(d.get('braco', 'afetado')), cadencia_alvo=float(d['cadencia_alvo']),
               amp_min=float(d.get('amp_min', 0)), amp_max=float(d.get('amp_max', 60)),
               duracao_min=float(d.get('duracao_min', 3)), reps_alvo=int(reps_alvo) if reps_alvo else None)
    if not 4 <= cfg['cadencia_alvo'] <= 30:
        raise ValueError('cadência fora de 4–30 rep/min')
    if cfg['braco'] not in ('afetado', 'saudavel'):
        raise ValueError('braço deve ser "afetado" ou "saudavel"')
    if not consulta('SELECT id FROM patients WHERE id = ?', cfg['patient_id']):
        abort(404)
    cur = db().execute('INSERT INTO sessions (patient_id, inicio, exercicio, exercicio_id, braco, cadencia_alvo, amp_min, '
                       'amp_max, duracao_min, reps_alvo) VALUES (:patient_id, :inicio, :exercicio, :exercicio_id, :braco, '
                       ':cadencia_alvo, :amp_min, :amp_max, :duracao_min, :reps_alvo)',
                       {**cfg, 'inicio': datetime.now().isoformat(timespec='seconds')})
    db().commit()
    sid = cur.lastrowid
    if gravacao:
        gravacao.close()
    gravacao = open(BRUTOS / f'sessao_{sid}.csv', 'w', buffering=1)
    gravacao.write('t_ms,theta,ax,ay,az\n')
    jogo = (f"/game/?sessao={sid}&cadencia={cfg['cadencia_alvo']:g}&min={cfg['amp_min']:g}"
            f"&max={cfg['amp_max']:g}&duracao={cfg['duracao_min']:g}&reps={cfg['reps_alvo'] or ''}")
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
    """Fecha a sessão: consolida as repetições, roda o módulo de análise e guarda tudo no histórico."""
    global gravacao
    sessao_aberta(sid)
    if gravacao:
        gravacao.close()
        gravacao = None
    d = request.get_json(silent=True) or {}
    m = db().execute('SELECT COUNT(*) AS total_reps, ROUND(AVG(amplitude), 1) AS amp_media, '
                     'ROUND(AVG(vel_pico), 1) AS vel_media, ROUND(AVG(no_ritmo), 3) AS aderencia_cadencia '
                     'FROM reps WHERE session_id = ?', (sid,)).fetchone()
    analise = analisar(sid)
    resumo = {k: analise[k] for k in ('metricas', 'notas', 'score_geral', 'sessao_saudavel')} if analise else None
    resultado = {**dict(m), 'id': sid, 'fim': datetime.now().isoformat(timespec='seconds'),
                 'duracao_s': d.get('duracao_s'), 'obstaculos': d.get('obstaculos'), 'pontuacao': d.get('pontuacao'),
                 'score_geral': resumo and resumo['score_geral'], 'analise': resumo and json.dumps(resumo)}
    db().execute('UPDATE sessions SET fim = :fim, duracao_s = :duracao_s, total_reps = :total_reps, '
                 'amp_media = :amp_media, vel_media = :vel_media, aderencia_cadencia = :aderencia_cadencia, '
                 'obstaculos = :obstaculos, pontuacao = :pontuacao, score_geral = :score_geral, analise = :analise '
                 'WHERE id = :id', resultado)
    db().commit()
    return jsonify({**resultado, 'analise': resumo})


@app.get('/api/sessions/<int:sid>')
@login_obrigatorio
def detalhe_sessao(sid):
    """Sessão com repetições e análise completa (inclui séries para os gráficos)."""
    sessao = consulta(SQL_SESSAO + 'WHERE s.id = ?', sid)
    if not sessao:
        abort(404)
    sessao = sessao[0]
    sessao['analise'] = analisar(sid) if sessao['fim'] else None
    return jsonify(**sessao, reps=consulta('SELECT * FROM reps WHERE session_id = ? ORDER BY n', sid))


# =====================================================================
# 6. FRONTEND
# =====================================================================
@app.get('/')
def inicio():
    return send_from_directory(FRONT, 'index.html')


@app.get('/game/')
def jogo():
    return send_from_directory(FRONT / 'game', 'index.html')


# =====================================================================
if __name__ == '__main__':
    BRUTOS.mkdir(parents=True, exist_ok=True)
    inicializar_banco()
    threading.Thread(target=lambda: asyncio.run(laco_bluetooth()), daemon=True).start()
    app.run(port=5000, threaded=True)
