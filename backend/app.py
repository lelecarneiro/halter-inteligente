"""
Backend do Halter Inteligente

1. Conecta ao haltere por Bluetooth (BLE) e repassa cada leitura ao jogo em tempo real (Server-Sent Events)
2. Guarda pacientes, sessões (com séries dos dois braços) e repetições em SQLite
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
gravacao_sid = None        # id da sessão que está sendo gravada
gravacao_simulada = False  # True quando os dados vêm do modo simulado do jogo (ignora o Bluetooth)


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
    if arquivo and not gravacao_simulada:
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
#    sessão (configuração) → séries (cada uma com um braço) → repetições
# =====================================================================
ESQUEMA = '''
CREATE TABLE IF NOT EXISTS exercicios (
  id INTEGER PRIMARY KEY, nome TEXT NOT NULL UNIQUE COLLATE NOCASE, descricao TEXT, padrao INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS patients (
  id INTEGER PRIMARY KEY, nome TEXT NOT NULL, nascimento TEXT, observacoes TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id), inicio TEXT, fim TEXT,
  exercicio TEXT, exercicio_id INTEGER REFERENCES exercicios(id),
  series_alvo INTEGER, reps_alvo INTEGER, ordem TEXT,                                         -- configuração
  cadencia_alvo REAL, amp_min REAL, amp_max REAL, duracao_min REAL,
  score_geral REAL, analise TEXT);                                -- score do braço afetado + resumo por braço (JSON)
CREATE TABLE IF NOT EXISTS series (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id), n INTEGER, braco TEXT NOT NULL,
  inicio TEXT, fim TEXT, cadencia REAL, amp_min REAL, amp_max REAL,                         -- valores usados na série
  duracao_s REAL, total_reps INTEGER, amp_media REAL, vel_media REAL, aderencia_cadencia REAL,
  obstaculos TEXT, pontuacao INTEGER, score_geral REAL, analise TEXT);
CREATE TABLE IF NOT EXISTS reps (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id), serie_id INTEGER REFERENCES series(id),
  n INTEGER, t_ms INTEGER, amplitude REAL, vel_pico REAL, duracao_s REAL, no_ritmo INTEGER, cadencia REAL);
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
BRACOS = ('saudavel', 'afetado')


def inicializar_banco():
    """Cria as tabelas e categorias padrão e atualiza bancos de versões anteriores sem perder dados."""
    with sqlite3.connect(BANCO) as con:
        con.executescript(ESQUEMA)
        colunas = lambda tabela: {c[1] for c in con.execute(f'PRAGMA table_info({tabela})')}
        antigas = colunas('sessions')
        for coluna, tipo in [('exercicio_id', 'INTEGER'), ('series_alvo', 'INTEGER'), ('reps_alvo', 'INTEGER'),
                             ('ordem', 'TEXT'), ('score_geral', 'REAL'), ('analise', 'TEXT'), ('braco', 'TEXT')]:
            if coluna not in antigas:
                con.execute(f'ALTER TABLE sessions ADD COLUMN {coluna} {tipo}')
        if 'serie_id' not in colunas('reps'):
            con.execute('ALTER TABLE reps ADD COLUMN serie_id INTEGER REFERENCES series(id)')
        con.executemany('INSERT OR IGNORE INTO exercicios (nome, descricao, padrao) VALUES (?, ?, 1)', EXERCICIOS_PADRAO)

        # Versões antigas: exercício em texto livre → categoria
        con.execute("UPDATE sessions SET exercicio = 'Flexão de cotovelo' WHERE exercicio IS NULL OR TRIM(exercicio) = ''")
        con.execute('INSERT OR IGNORE INTO exercicios (nome) SELECT DISTINCT exercicio FROM sessions WHERE exercicio_id IS NULL')
        con.execute('UPDATE sessions SET exercicio_id = (SELECT id FROM exercicios e WHERE e.nome = sessions.exercicio) '
                    'WHERE exercicio_id IS NULL')

        # Versões antigas: uma sessão = um braço → vira uma sessão com uma única série
        migrar = [linha[0] for linha in con.execute('SELECT id FROM sessions WHERE series_alvo IS NULL')]
        if migrar:
            copiar = [c for c in ('duracao_s', 'total_reps', 'amp_media', 'vel_media', 'aderencia_cadencia',
                                  'obstaculos', 'pontuacao') if c in antigas]
            extra = ''.join(', ' + c for c in copiar)
            con.execute(f'INSERT INTO series (session_id, n, braco, inicio, fim, cadencia, amp_min, amp_max{extra}) '
                        f"SELECT id, 1, COALESCE(braco, 'afetado'), inicio, fim, cadencia_alvo, amp_min, amp_max{extra} "
                        'FROM sessions WHERE series_alvo IS NULL')
            con.execute('UPDATE reps SET serie_id = (SELECT MIN(id) FROM series WHERE series.session_id = reps.session_id) '
                        'WHERE serie_id IS NULL')
            con.execute('UPDATE sessions SET series_alvo = 1 WHERE series_alvo IS NULL')
    with app.app_context():                                      # recalcula a análise das sessões migradas
        for sid in migrar:
            if consulta('SELECT fim FROM sessions WHERE id = ?', sid)[0]['fim']:
                for s in consulta('SELECT id FROM series WHERE session_id = ?', sid):
                    salvar_analise_serie(s['id'])
                salvar_analise_sessao(sid)
        for s in consulta('SELECT id, analise FROM sessions WHERE fim IS NOT NULL'):   # resumos de versões antigas
            if json.loads(s['analise'] or '{}').get('versao') != VERSAO_ANALISE:
                salvar_analise_sessao(s['id'])


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


def falha(codigo, mensagem):
    """Interrompe a requisição com uma mensagem de erro legível pela plataforma."""
    abort(make_response(jsonify(erro=mensagem), codigo))


def sessao_aberta(sid):
    linha = consulta('SELECT * FROM sessions WHERE id = ?', sid)
    if not linha:
        abort(404)
    if linha[0]['fim']:
        falha(409, 'Esta sessão já foi encerrada.')
    return linha[0]


def serie_aberta(serie_id):
    linha = consulta('SELECT * FROM series WHERE id = ?', serie_id)
    if not linha:
        abort(404)
    if linha[0]['fim']:
        falha(409, 'Esta série já foi encerrada.')
    return linha[0]


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
FASE0 = 2 * np.pi - np.arccos(0.4)   # cada repetição começa ao cruzar 30 % da amplitude, descendo
# 5.10 Erro a partir do qual a nota vale 0. Valores iniciais: calibrar com voluntários saudáveis.
ERRO_MAX = {'trajetoria': 15.0,     # RMSE da trajetória vs. ideal (°)
            'simetria': 15.0,       # RMSE braço afetado vs. saudável (°)
            'estabilidade': 0.05,   # desvio padrão da posição lateral Sx (m)
            'suavidade': 40.0}      # jerk médio = I_suavidade / duração (m/s³)
PESOS = {'trajetoria': 0.30, 'simetria': 0.30, 'estabilidade': 0.25, 'suavidade': 0.15}
VERSAO_ANALISE = 2               # aumentar quando o resumo ganhar campos novos: as sessões antigas são recalculadas


def trapezio(y, t):
    """5.5 Integral acumulada pelo método dos trapézios: I(t_k) = I(t_k-1) + Δt/2 · [y(t_k-1) + y(t_k)]."""
    return np.concatenate([[0.0], np.cumsum(np.diff(t) / 2 * (y[1:] + y[:-1]))])


def media_movel(x, k=K_MEDIA):
    """5.7 Média das últimas K amostras: x_lenta(t_k) = (1/K) · Σ x(t_k-j), j = 0…K-1."""
    soma = np.concatenate([[0.0], np.cumsum(x)])
    n = np.arange(1, len(x) + 1)
    inicio = np.maximum(0, n - k)
    return (soma[n] - soma[inicio]) / (n - inicio)


def rmse(a, b):
    return float(np.sqrt(np.mean((np.asarray(a) - np.asarray(b)) ** 2)))


def nota(erro, erro_max):
    """5.10 Nota_i = 100 · (1 − Erro_medido / Erro_max), limitada entre 0 e 100."""
    return float(np.clip(100 * (1 - erro / erro_max), 0, 100))


def notas_e_score(metricas, rmse_simetria=None):
    """5.10 Notas de 0 a 100 e score geral ponderado (pesos redistribuídos se faltar alguma nota)."""
    erros = {'trajetoria': metricas['rmse_trajetoria'], 'simetria': rmse_simetria,
             'estabilidade': metricas['s_x'], 'suavidade': metricas['jerk_medio']}
    notas = {k: nota(e, ERRO_MAX[k]) for k, e in erros.items() if e is not None and np.isfinite(e)}
    score = sum(PESOS[k] * n for k, n in notas.items()) / sum(PESOS[k] for k in notas)
    return {k: arred(v, 1) for k, v in notas.items()}, arred(score, 1)


def arred(x, casas=3):
    """Número (ou lista/dicionário) pronto para JSON: arredondado e sem NaN."""
    if isinstance(x, dict):
        return {k: arred(v, casas) for k, v in x.items()}
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


def janelas(serie, reps):
    """Intervalo de tempo (s, relógio do ESP32) de cada repetição: do fim da anterior até o fim desta."""
    periodo = 60 / (serie['cadencia'] or 12)
    fins = [r['t_ms'] / 1000 for r in reps]
    return list(zip([fins[0] - (reps[0]['duracao_s'] or periodo)] + fins[:-1], fins))


def analisar_grupo(dados, grupos, series_temporais=False):
    """Aplica os métodos numéricos a um conjunto de séries (uma série, ou todas as de um braço).

    grupos: lista de (serie, reps). O movimento é medido como EXCURSÃO a partir da posição baixa, no sentido
    da subida — assim os dois braços são comparáveis mesmo que o sensor fique invertido em uma das mãos.
    """
    t_total = dados[:, 0] / 1000
    tau = np.linspace(0, 1, M_PONTOS)
    i_suavidade = duracao = 0.0
    residuos, laterais, vel_pico, curvas, ideais, erros_traj = [], [], [], [], [], []
    sinais = None
    for serie, reps in grupos:
        if not reps:
            continue
        jan = janelas(serie, reps)
        no_trecho = (t_total >= jan[0][0]) & (t_total <= jan[-1][1])
        if no_trecho.sum() < 10:
            continue
        t, theta, ax, ay, az = (c[no_trecho] for c in (t_total, *dados[:, 1:5].T))

        # 5.4.3 Módulo da aceleração linear · 5.6 jerk por diferenças finitas e índice de suavidade
        a_lin = np.hypot(ay, az)
        jerk = np.diff(a_lin) / np.diff(t)
        i_suavidade += trapezio(np.abs(jerk), t[1:])[-1]
        duracao += t[-1] - t[0]
        # 5.7 Média móvel (K = 10) e resíduo → tremor
        a_lenta = media_movel(a_lin)
        residuos.append(a_lin - a_lenta)

        sentido = np.sign(serie['amp_max'] - serie['amp_min']) or 1.0
        ideal = abs(serie['amp_max'] - serie['amp_min']) * (0.5 - 0.5 * np.cos(FASE0 + 2 * np.pi * tau))
        px = np.full_like(t, np.nan)
        for (ini, fim), rep in zip(jan, reps):
            j = (t >= ini) & (t <= fim)
            if j.sum() < 5:
                continue
            tr = t[j]
            frac = (tr - tr[0]) / (tr[-1] - tr[0])
            # 5.5 Trapézios: remove o viés de cada eixo e força v = 0 no início e no fim da repetição
            v = np.column_stack([trapezio(a - a.mean(), tr) for a in (ax[j], ay[j], az[j])])
            v -= np.outer(frac, v[-1])
            p = np.column_stack([trapezio(v[:, k], tr) for k in range(3)])
            px[j] = p[:, 0]
            vel_pico.append(np.linalg.norm(v, axis=1).max())
            # 5.9 Tempo normalizado τ = t / T_total e interpolação linear. A 1ª repetição da série parte do
            # repouso (não tem um ciclo completo antes dela), então fica fora da comparação de trajetórias.
            if rep['duracao_s'] is None:
                continue
            curva = np.interp(tau, frac, sentido * (theta[j] - serie['amp_min']))
            curvas.append(curva)
            ideais.append(ideal)
            erros_traj.append(rmse(curva, ideal))
        laterais.append(px[np.isfinite(px)])
        if series_temporais:
            sinais = dict(t=t - t[0], theta=theta, theta_suave=media_movel(theta), a_lin=a_lin, a_lenta=a_lenta,
                          tremor=a_lin - a_lenta, jerk=np.concatenate([[0.0], jerk]), px=px)
    if not curvas:
        return None

    # 5.8 Compensação postural: média, variância e desvio padrão amostral da posição lateral (eixo X)
    px_todos = np.concatenate(laterais)
    var_x = px_todos.var(ddof=1) if len(px_todos) > 1 else 0.0
    tremor = np.concatenate(residuos)
    curva_media = np.mean(curvas, axis=0)
    metricas = dict(duracao_s=duracao, i_suavidade=i_suavidade, jerk_medio=i_suavidade / duracao,
                    rms_tremor=np.sqrt(np.mean(tremor ** 2)), mu_x=px_todos.mean(), var_x=var_x, s_x=np.sqrt(var_x),
                    vel_linear_pico=np.mean(vel_pico), rmse_trajetoria=np.mean(erros_traj),
                    excursao=curva_media.max() - curva_media.min())
    resultado = {'metricas': arred(metricas), 'curva_media': arred(curva_media, 2),
                 'curva_ideal': arred(np.mean(ideais, axis=0), 2)}
    if sinais:
        passo = max(1, len(sinais['t']) // 1500)                    # séries reduzidas para os gráficos
        resultado['sinais'] = {k: arred(v[::passo]) for k, v in sinais.items()}
    return resultado


def reps_da_serie(serie_id):
    return consulta('SELECT * FROM reps WHERE serie_id = ? ORDER BY n', serie_id)


def analisar_serie(serie_id, sinais=True):
    serie = consulta('SELECT * FROM series WHERE id = ?', serie_id)[0]
    dados = ler_brutos(serie['session_id'])
    reps = reps_da_serie(serie_id)
    a = analisar_grupo(dados, [(serie, reps)], series_temporais=sinais) if dados is not None and reps else None
    if a:
        a['notas'], a['score_geral'] = notas_e_score(a['metricas'])
    return a


def analisar_sessao(sid):
    """Resume cada braço (todas as suas séries juntas) e compara os dois braços da mesma sessão."""
    dados = ler_brutos(sid)
    series = consulta('SELECT * FROM series WHERE session_id = ? ORDER BY n, braco', sid)
    resumo = {'versao': VERSAO_ANALISE, 'bracos': {}, 'simetria': None, 'score_geral': None}
    for braco in BRACOS:
        # tempo_relativo = duração média da repetição ÷ tempo alvo (1 = no ritmo, 1,2 = 20 % mais lento)
        jogo = consulta('SELECT COUNT(r.id) AS total_reps, ROUND(AVG(r.amplitude), 1) AS amp_media, '
                        'ROUND(AVG(r.vel_pico), 1) AS vel_media, ROUND(AVG(r.no_ritmo), 3) AS aderencia_cadencia, '
                        'ROUND(AVG(r.duracao_s * r.cadencia / 60.0), 3) AS tempo_relativo '
                        'FROM reps r JOIN series s ON s.id = r.serie_id WHERE s.session_id = ? AND s.braco = ?', sid, braco)[0]
        do_braco = [s for s in series if s['braco'] == braco]
        jogo.update(series=len(do_braco), pontuacao=sum(s['pontuacao'] or 0 for s in do_braco))
        grupos = [(s, reps_da_serie(s['id'])) for s in do_braco]
        analise = analisar_grupo(dados, grupos) if dados is not None else None
        # meta de amplitude do braço = média das amplitudes configuradas nas suas séries
        alvo = np.mean([abs(s['amp_max'] - s['amp_min']) for s in do_braco]) if do_braco else None
        jogo['amplitude_alvo'] = arred(alvo, 1)
        if analise and alvo:
            analise['metricas']['amplitude_pct'] = arred(100 * analise['metricas']['excursao'] / alvo, 1)
        resumo['bracos'][braco] = {'jogo': jogo, **(analise or {})}

    afetado, saudavel = resumo['bracos']['afetado'], resumo['bracos']['saudavel']
    rmse_sim = None
    if 'curva_media' in afetado and 'curva_media' in saudavel:
        # 5.9 Comparação entre braços: curvas normalizadas no tempo, RMSE e atraso do ápice
        ca, cs = np.array(afetado['curva_media'], float), np.array(saudavel['curva_media'], float)
        rmse_sim = rmse(ca, cs)
        exc_s = saudavel['metricas']['excursao']
        resumo['simetria'] = {'rmse': arred(rmse_sim), 'atraso_apice_pct': arred(np.argmax(ca) - np.argmax(cs), 1),
                              'excursao_pct': arred(100 * afetado['metricas']['excursao'] / exc_s, 1) if exc_s else None}
    for braco, r in resumo['bracos'].items():
        if 'metricas' in r:
            r['notas'], r['score_geral'] = notas_e_score(r['metricas'], rmse_sim if braco == 'afetado' else None)
    resumo['score_geral'] = afetado.get('score_geral')
    return resumo


def salvar_analise_serie(serie_id):
    a = analisar_serie(serie_id, sinais=False)
    db().execute('UPDATE series SET score_geral = ?, analise = ? WHERE id = ?',
                 (a and a['score_geral'], a and json.dumps({k: a[k] for k in ('metricas', 'notas', 'score_geral')}), serie_id))
    db().commit()
    return a


def salvar_analise_sessao(sid):
    resumo = analisar_sessao(sid)
    db().execute('UPDATE sessions SET score_geral = ?, analise = ? WHERE id = ?', (resumo['score_geral'], json.dumps(resumo), sid))
    db().commit()
    return resumo


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
# 5. API — PACIENTES E EXERCÍCIOS
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
                       (nome, d.get('nascimento') or None, d.get('observacoes')))
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
    """Exclui o paciente com todas as sessões, séries, repetições e leituras brutas."""
    global gravacao, gravacao_sid
    if not consulta('SELECT id FROM patients WHERE id = ?', pid):
        abort(404)
    sessoes = [s['id'] for s in consulta('SELECT id FROM sessions WHERE patient_id = ?', pid)]
    for sid in sessoes:
        if gravacao and gravacao_sid == sid:
            gravacao.close()
            gravacao, gravacao_sid = None, None
        (BRUTOS / f'sessao_{sid}.csv').unlink(missing_ok=True)
    filtro = 'SELECT id FROM sessions WHERE patient_id = ?'
    db().execute(f'DELETE FROM reps WHERE session_id IN ({filtro})', (pid,))
    db().execute(f'DELETE FROM series WHERE session_id IN ({filtro})', (pid,))
    db().execute('DELETE FROM sessions WHERE patient_id = ?', (pid,))
    db().execute('DELETE FROM patients WHERE id = ?', (pid,))
    db().commit()
    return jsonify(ok=True, sessoes_excluidas=len(sessoes))


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


# =====================================================================
# 6. API — SESSÕES, SÉRIES E REPETIÇÕES
#    Fluxo do jogo: cria sessão → para cada série e braço: cria série → envia repetições → encerra série
#                   → encerra sessão (análise por braço e comparação entre os braços)
# =====================================================================
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
    global gravacao, gravacao_sid, gravacao_simulada
    d = request.get_json(force=True)
    exercicio = consulta('SELECT id, nome FROM exercicios WHERE id = ?', int(d['exercicio_id']))
    if not exercicio:
        falha(404, 'Categoria de exercício não encontrada.')
    reps_alvo = d.get('reps_alvo')
    cfg = dict(patient_id=int(d['patient_id']), exercicio_id=exercicio[0]['id'], exercicio=exercicio[0]['nome'],
               series_alvo=int(d.get('series_alvo') or 3), reps_alvo=int(reps_alvo) if reps_alvo else None,
               ordem=str(d.get('ordem') or 'saudavel'), cadencia_alvo=float(d['cadencia_alvo']),
               amp_min=float(d.get('amp_min', 0)), amp_max=float(d.get('amp_max', 60)),
               duracao_min=float(d.get('duracao_min') or 2))
    if not 4 <= cfg['cadencia_alvo'] <= 30:
        raise ValueError('cadência fora de 4–30 rep/min')
    if not 1 <= cfg['series_alvo'] <= 10:
        raise ValueError('número de séries fora de 1–10')
    if cfg['ordem'] not in BRACOS:
        raise ValueError('ordem deve começar pelo braço "saudavel" ou "afetado"')
    if not consulta('SELECT id FROM patients WHERE id = ?', cfg['patient_id']):
        abort(404)
    cur = db().execute('INSERT INTO sessions (patient_id, inicio, exercicio, exercicio_id, series_alvo, reps_alvo, ordem, '
                       'cadencia_alvo, amp_min, amp_max, duracao_min) VALUES (:patient_id, :inicio, :exercicio, '
                       ':exercicio_id, :series_alvo, :reps_alvo, :ordem, :cadencia_alvo, :amp_min, :amp_max, :duracao_min)',
                       {**cfg, 'inicio': datetime.now().isoformat(timespec='seconds')})
    db().commit()
    sid = cur.lastrowid
    if gravacao:
        gravacao.close()
    gravacao = open(BRUTOS / f'sessao_{sid}.csv', 'w', buffering=1)
    gravacao.write('t_ms,theta,ax,ay,az\n')
    gravacao_sid, gravacao_simulada = sid, False
    jogo = (f"/game/?sessao={sid}&cadencia={cfg['cadencia_alvo']:g}&min={cfg['amp_min']:g}&max={cfg['amp_max']:g}"
            f"&duracao={cfg['duracao_min']:g}&reps={cfg['reps_alvo'] or ''}&series={cfg['series_alvo']}&ordem={cfg['ordem']}")
    return jsonify(id=sid, jogo=jogo), 201


@app.post('/api/sessions/<int:sid>/series')
@login_obrigatorio
def criar_serie(sid):
    """O jogo avisa que uma série vai começar (número, braço e os valores de cadência/amplitude em uso)."""
    sessao = sessao_aberta(sid)
    d = request.get_json(force=True)
    braco = str(d['braco'])
    if braco not in BRACOS:
        raise ValueError('braço deve ser "saudavel" ou "afetado"')
    cur = db().execute('INSERT INTO series (session_id, n, braco, inicio, cadencia, amp_min, amp_max) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (sid, int(d['n']), braco, datetime.now().isoformat(timespec='seconds'),
                        float(d.get('cadencia') or sessao['cadencia_alvo']), float(d.get('amp_min', sessao['amp_min'])),
                        float(d.get('amp_max', sessao['amp_max']))))
    db().commit()
    return jsonify(id=cur.lastrowid), 201


@app.post('/api/series/<int:serie_id>/reps')
@login_obrigatorio
def salvar_repeticao(serie_id):
    """Valida e armazena cada repetição enviada pelo jogo."""
    serie = serie_aberta(serie_id)
    d = request.get_json(force=True)
    opcional = lambda k, tipo: None if d.get(k) is None else tipo(d[k])
    rep = dict(session_id=serie['session_id'], serie_id=serie_id, n=int(d['n']), t_ms=int(d['t_ms']),
               amplitude=float(d['amplitude']), vel_pico=float(d['vel_pico']), duracao_s=opcional('duracao_s', float),
               no_ritmo=opcional('no_ritmo', bool), cadencia=float(d['cadencia']))
    if not 0 <= rep['amplitude'] <= 360 or rep['vel_pico'] < 0:
        raise ValueError('amplitude ou velocidade fora da faixa')
    db().execute('INSERT INTO reps (session_id, serie_id, n, t_ms, amplitude, vel_pico, duracao_s, no_ritmo, cadencia) '
                 'VALUES (:session_id, :serie_id, :n, :t_ms, :amplitude, :vel_pico, :duracao_s, :no_ritmo, :cadencia)', rep)
    db().commit()
    return jsonify(ok=True), 201


@app.post('/api/series/<int:serie_id>/end')
@login_obrigatorio
def encerrar_serie(serie_id):
    """Consolida a série, roda a análise dela e devolve o resultado para o jogo mostrar."""
    serie_aberta(serie_id)
    d = request.get_json(silent=True) or {}
    m = consulta('SELECT COUNT(*) AS total_reps, ROUND(AVG(amplitude), 1) AS amp_media, ROUND(AVG(vel_pico), 1) AS vel_media, '
                 'ROUND(AVG(no_ritmo), 3) AS aderencia_cadencia FROM reps WHERE serie_id = ?', serie_id)[0]
    db().execute('UPDATE series SET fim = :fim, duracao_s = :duracao_s, total_reps = :total_reps, amp_media = :amp_media, '
                 'vel_media = :vel_media, aderencia_cadencia = :aderencia_cadencia, obstaculos = :obstaculos, '
                 'pontuacao = :pontuacao WHERE id = :id',
                 {**m, 'id': serie_id, 'fim': datetime.now().isoformat(timespec='seconds'), 'duracao_s': d.get('duracao_s'),
                  'obstaculos': d.get('obstaculos'), 'pontuacao': d.get('pontuacao')})
    db().commit()
    a = salvar_analise_serie(serie_id)
    return jsonify(**m, score_geral=a and a['score_geral'], notas=a and a['notas'])


@app.post('/api/sessions/<int:sid>/brutos')
@login_obrigatorio
def receber_brutos(sid):
    """Leituras geradas pelo modo simulado do jogo, gravadas como se viessem do haltere."""
    global gravacao_simulada
    sessao_aberta(sid)
    if not gravacao or gravacao_sid != sid:
        falha(409, 'Esta sessão não está sendo gravada.')
    gravacao_simulada = True                               # a partir daqui, ignora o Bluetooth nesta sessão
    validas = []
    for linha in request.get_json(force=True)['linhas'][:5000]:
        partes = str(linha).split(',')
        if len(partes) == 5 and all(np.isfinite(float(p)) for p in partes):
            validas.append(','.join(partes))
    gravacao.write(''.join(l + '\n' for l in validas))
    return jsonify(gravadas=len(validas))


@app.post('/api/sessions/<int:sid>/end')
@login_obrigatorio
def encerrar_sessao(sid):
    """Fecha a sessão: resume cada braço, compara os dois e guarda tudo no histórico."""
    global gravacao, gravacao_sid
    sessao_aberta(sid)
    if gravacao and gravacao_sid == sid:
        gravacao.close()
        gravacao, gravacao_sid = None, None
    for s in consulta('SELECT id FROM series WHERE session_id = ? AND fim IS NULL', sid):   # série interrompida
        db().execute('UPDATE series SET fim = ?, total_reps = (SELECT COUNT(*) FROM reps WHERE serie_id = ?) WHERE id = ?',
                     (datetime.now().isoformat(timespec='seconds'), s['id'], s['id']))
        salvar_analise_serie(s['id'])
    db().execute('UPDATE sessions SET fim = ? WHERE id = ?', (datetime.now().isoformat(timespec='seconds'), sid))
    db().commit()
    resumo = salvar_analise_sessao(sid)
    return jsonify(id=sid, **resumo)


@app.get('/api/sessions/<int:sid>')
@login_obrigatorio
def detalhe_sessao(sid):
    """Sessão com as séries, as repetições e a análise por braço (recalculada)."""
    sessao = consulta(SQL_SESSAO + 'WHERE s.id = ?', sid)
    if not sessao:
        abort(404)
    sessao = sessao[0]
    series = consulta('SELECT * FROM series WHERE session_id = ? ORDER BY n, inicio', sid)
    for s in series:
        s['analise'] = json.loads(s['analise']) if s['analise'] else None
    sessao['analise'] = analisar_sessao(sid) if sessao['fim'] else None
    return jsonify({**sessao, 'series': series, 'reps': consulta('SELECT * FROM reps WHERE session_id = ? ORDER BY serie_id, n', sid)})


@app.get('/api/series/<int:serie_id>')
@login_obrigatorio
def detalhe_serie(serie_id):
    """Uma série com as repetições e os sinais (θ, aceleração, tremor, jerk, posição lateral) para os gráficos."""
    serie = consulta('SELECT * FROM series WHERE id = ?', serie_id)
    if not serie:
        abort(404)
    return jsonify({**serie[0], 'reps': reps_da_serie(serie_id), 'analise': analisar_serie(serie_id)})


# =====================================================================
# 7. FRONTEND
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
