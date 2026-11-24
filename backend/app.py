"""
Backend do Halter Inteligente

1. Conecta ao haltere por Bluetooth (BLE) e repassa cada leitura ao jogo em tempo real (Server-Sent Events)
2. Guarda pacientes, sessões (com séries dos dois braços) e repetições em SQLite
3. Módulo de análise: aplica os métodos numéricos do memorial de cálculo às leituras de cada sessão
4. Exporta os dados de cada paciente (CSV + gráficos) em um .zip
5. Serve a plataforma do profissional (/) e o jogo (/game/)

Rodar:  python backend/app.py   →  http://localhost:5000
Login padrão: fisio / halter123 (troque com as variáveis HALTER_USUARIO e HALTER_SENHA)
"""
import asyncio
import csv
import io
import json
import os
import queue
import re
import sqlite3
import threading
import unicodedata
import warnings
import zipfile
from datetime import datetime
from functools import wraps
from pathlib import Path

import numpy as np
from bleak import BleakClient, BleakScanner
from flask import Flask, Response, abort, g, jsonify, make_response, request, send_file, send_from_directory, session
from matplotlib.patches import Patch

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
CREATE TABLE IF NOT EXISTS jogadores (                                                     -- progresso no jogo
  patient_id INTEGER PRIMARY KEY REFERENCES patients(id), xp INTEGER DEFAULT 0, conquistas TEXT, visual TEXT, atualizado TEXT);
CREATE TABLE IF NOT EXISTS reps (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id), serie_id INTEGER REFERENCES series(id),
  n INTEGER, t_ms INTEGER, amplitude REAL, vel_pico REAL, duracao_s REAL, no_ritmo INTEGER, cadencia REAL);
'''

# Únicos exercícios disponíveis para novas sessões
EXERCICIOS_PADRAO = [
    ('Flexão de cotovelo', 'Rosca bíceps: dobrar o cotovelo trazendo o haltere em direção ao ombro'),
    ('Flexão de punho', 'Antebraço apoiado, palma para cima: dobrar o punho'),
    ('Elevação lateral', 'Abdução de ombro: elevar o braço estendido para o lado até a altura do ombro'),
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
        con.executemany('UPDATE exercicios SET padrao = 1 WHERE nome = ?', [(n,) for n, _ in EXERCICIOS_PADRAO])

        # Versões antigas: exercício em texto livre → categoria
        con.execute("UPDATE sessions SET exercicio = 'Flexão de cotovelo' WHERE exercicio IS NULL OR TRIM(exercicio) = ''")
        con.execute('INSERT OR IGNORE INTO exercicios (nome) SELECT DISTINCT exercicio FROM sessions WHERE exercicio_id IS NULL')
        con.execute('UPDATE sessions SET exercicio_id = (SELECT id FROM exercicios e WHERE e.nome = sessions.exercicio) '
                    'WHERE exercicio_id IS NULL')
        # Outros exercícios: deixam de estar disponíveis; os que já têm sessões ficam só para o histórico
        nomes = [n for n, _ in EXERCICIOS_PADRAO]
        marcas = ','.join('?' * len(nomes))
        con.execute(f'UPDATE exercicios SET padrao = 0 WHERE nome NOT IN ({marcas})', nomes)
        con.execute(f'DELETE FROM exercicios WHERE nome NOT IN ({marcas}) AND id NOT IN '
                    '(SELECT exercicio_id FROM sessions WHERE exercicio_id IS NOT NULL)', nomes)

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
                for se in consulta('SELECT id FROM series WHERE session_id = ?', s['id']):
                    salvar_analise_serie(se['id'])
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
FASE0 = 2 * np.pi - np.arccos(0.4)   # a janela do jogo começa ao cruzar 30 % da amplitude, descendo
# 5.10 Erro a partir do qual a nota vale 0. Valores iniciais: calibrar com voluntários saudáveis.
ERRO_MAX = {'trajetoria': 15.0,     # RMSE da trajetória vs. ideal (°)
            'simetria': 15.0,       # RMSE braço afetado vs. saudável (°)
            'suavidade': 40.0}      # jerk médio = I_suavidade / duração (m/s³)
PESOS = {'trajetoria': 0.40, 'simetria': 0.40, 'suavidade': 0.20}
VERSAO_ANALISE = 4               # aumentar quando o resumo ganhar campos novos: as sessões antigas são recalculadas


def trapezio(y, t):
    """5.5 Integral acumulada pelo método dos trapézios: I(t_k) = I(t_k-1) + Δt/2 · [y(t_k-1) + y(t_k)]."""
    return np.concatenate([[0.0], np.cumsum(np.diff(t) / 2 * (y[1:] + y[:-1]))])


def media_movel(x, k=K_MEDIA):
    """5.7 Média das últimas K amostras: x_lenta(t_k) = (1/K) · Σ x(t_k-j), j = 0…K-1."""
    soma = np.concatenate([[0.0], np.cumsum(x)])
    n = np.arange(1, len(x) + 1)
    inicio = np.maximum(0, n - k)
    return (soma[n] - soma[inicio]) / (n - inicio)


def interpolacao_linear(tau, tau_amostras, p_amostras):
    """5.9 Interpolação linear de Lagrange entre os dois pontos vizinhos (τ0 ≤ τ ≤ τ1):
    p_interp(τ) = p(τ0) + [(p(τ1) − p(τ0)) / (τ1 − τ0)] · (τ − τ0)"""
    tau_amostras, p_amostras = np.asarray(tau_amostras, float), np.asarray(p_amostras, float)
    i1 = np.clip(np.searchsorted(tau_amostras, tau, side='right'), 1, len(tau_amostras) - 1)
    i0 = i1 - 1
    t0, t1, p0, p1 = tau_amostras[i0], tau_amostras[i1], p_amostras[i0], p_amostras[i1]
    passo = np.where(t1 > t0, t1 - t0, 1.0)                     # amostras repetidas no tempo: evita divisão por 0
    inclinacao = np.where(t1 > t0, (p1 - p0) / passo, 0.0)
    return p0 + inclinacao * (np.clip(tau, tau_amostras[0], tau_amostras[-1]) - t0)


def rmse(a, b):
    """5.9 RMSE = √[(1/M) · Σ (p_paciente(τ_i) − p_referência(τ_i))²]"""
    return float(np.sqrt(np.mean((np.asarray(a) - np.asarray(b)) ** 2)))


def nota(erro, erro_max):
    """5.10 Nota_i = 100 · (1 − Erro_medido / Erro_max), limitada entre 0 e 100."""
    return float(np.clip(100 * (1 - erro / erro_max), 0, 100))


def notas_e_score(metricas, rmse_simetria=None):
    """5.10 Notas de 0 a 100 e score geral ponderado (pesos redistribuídos se faltar alguma nota)."""
    erros = {'trajetoria': metricas['rmse_trajetoria'], 'simetria': rmse_simetria,
             'suavidade': metricas['jerk_medio']}
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
    if len(d) < 10:
        return None
    d = d[np.argsort(d[:, 0], kind='stable')]
    d[:, 1] = np.rad2deg(np.unwrap(np.deg2rad(d[:, 1])))   # θ contínuo: remove saltos de 360° (gravações antigas)
    return d


def janelas(serie, reps):
    """Intervalo de tempo (s, relógio do ESP32) de cada repetição: do fim da anterior até o fim desta.
    Limitado a 2 períodos da cadência: uma pausa longa antes da repetição não entra na integração (evita deriva)."""
    periodo = 60 / (serie['cadencia'] or 12)
    fins = [r['t_ms'] / 1000 for r in reps]
    inicios = [fins[0] - (reps[0]['duracao_s'] or periodo)] + fins[:-1]
    return [(max(ini, fim - 2 * periodo), fim) for ini, fim in zip(inicios, fins)]


def ciclos_vale_a_vale(t, exc, jan, periodo):
    """Segmenta as repetições de VALE a VALE. O jogo marca o fim da repetição ao cruzar 30 % da amplitude
    descendo (meio da descida, velocidade máxima); para a análise, cada repetição vai do ponto mais baixo antes
    do seu pico ao ponto mais baixo depois dele — onde o haltere está parado (v ≈ 0) —, sem o tempo de espera embaixo.
    Devolve, para cada repetição, os índices (início, fim) em t, ou None se não houver dados suficientes."""
    picos = []
    for ini, fim in jan:
        j = np.flatnonzero((t >= ini) & (t <= fim))
        picos.append(j[np.argmax(exc[j])] if len(j) >= 5 else None)
    ciclos = []
    for k, pk in enumerate(picos):
        if pk is None:
            ciclos.append(None)
            continue
        ant = next((p for p in reversed(picos[:k]) if p is not None), None)
        prox = next((p for p in picos[k + 1:] if p is not None), None)
        a = max(ant if ant is not None else 0, np.searchsorted(t, t[pk] - periodo))          # busca até 1 período
        b = min(prox if prox is not None else len(t) - 1, np.searchsorted(t, t[pk] + periodo) - 1)
        i0 = a + np.argmin(exc[a:pk + 1])
        i1 = pk + np.argmin(exc[pk:b + 1])
        amp = exc[pk] - min(exc[i0], exc[i1])
        if amp <= 0 or max(exc[i0], exc[i1]) > exc[pk] - 0.7 * amp:     # vale que não chegou embaixo: descarta
            ciclos.append(None)
            continue
        # Tempo parado embaixo fica fora: o ciclo começa quando o haltere sai de baixo (10 % da amplitude)
        # e termina quando volta a ela
        lim0, lim1 = exc[i0] + 0.1 * (exc[pk] - exc[i0]), exc[i1] + 0.1 * (exc[pk] - exc[i1])
        i0 += np.flatnonzero(exc[i0:pk + 1] <= lim0)[-1]
        i1 = pk + np.flatnonzero(exc[pk:i1 + 1] <= lim1)[0]
        ciclos.append((i0, i1) if i1 - i0 >= 5 else None)
    return ciclos


def analisar_grupo(dados, grupos, series_temporais=False):
    """Aplica os métodos numéricos a um conjunto de séries (uma série, ou todas as de um braço).

    grupos: lista de (serie, reps). O movimento é medido como EXCURSÃO a partir da posição baixa, no sentido
    da subida — assim os dois braços são comparáveis mesmo que o sensor fique invertido em uma das mãos.
    """
    t_total = dados[:, 0] / 1000
    tau = np.linspace(0, 1, M_PONTOS)
    i_suavidade = duracao = 0.0
    residuos, vel_pico, curvas, ideais, erros_traj = [], [], [], [], []
    sinais = None
    for serie, reps in grupos:
        if not reps:
            continue
        jan = janelas(serie, reps)
        periodo = 60 / (serie['cadencia'] or 12)
        no_trecho = (t_total >= jan[0][0] - periodo) & (t_total <= jan[-1][1] + periodo)   # folga para os vales
        if no_trecho.sum() < 10:
            continue
        t, theta, ax, ay, az = (c[no_trecho] for c in (t_total, *dados[:, 1:5].T))

        # 5.4.3 Módulo da aceleração linear · 5.6 jerk por diferenças finitas e índice de suavidade
        a_lin = np.hypot(ay, az)
        jerk = np.diff(a_lin) / np.diff(t)
        a_lenta = media_movel(a_lin)                                     # 5.7 média móvel (K = 10)
        nucleo = (t >= jan[0][0]) & (t <= jan[-1][1])                    # só o intervalo das repetições (sem a folga)
        if nucleo.sum() >= 3:
            tn = t[nucleo]
            i_suavidade += trapezio(np.abs(np.diff(a_lin[nucleo]) / np.diff(tn)), tn[1:])[-1]
            duracao += tn[-1] - tn[0]
            residuos.append((a_lin - a_lenta)[nucleo])                   # 5.7 resíduo → tremor

        sentido = np.sign(serie['amp_max'] - serie['amp_min']) or 1.0
        exc = sentido * (theta - serie['amp_min'])                       # excursão a partir da posição baixa (°)
        forma_ideal = 0.5 - 0.5 * np.cos(FASE0 + 2 * np.pi * tau)        # ciclo ideal de 0 a 1, na fase da janela do jogo
        # 5.5 Integração (velocidade): repetições de vale a vale, onde o haltere está parado (v ≈ 0)
        for ciclo in ciclos_vale_a_vale(t, exc, jan, periodo):
            if ciclo is None:
                continue
            j = slice(ciclo[0], ciclo[1] + 1)
            tr = t[j]
            frac = (tr - tr[0]) / (tr[-1] - tr[0])
            # Trapézios: remove o viés de cada eixo e força v = 0 nos vales (início e fim da repetição)
            v = np.column_stack([trapezio(a - a.mean(), tr) for a in (ax[j], ay[j], az[j])])
            v -= np.outer(frac, v[-1])
            vel_pico.append(np.linalg.norm(v, axis=1).max())
        # 5.9 Trajetória: janela do jogo (do cruzamento de 30 % descendo até o seguinte), na mesma fase da curva
        # ideal. Tempo normalizado τ = t / T_total e interpolação linear. A 1ª repetição da série parte do
        # repouso (não tem um ciclo completo antes dela), então fica fora da comparação.
        for (ini, fim), rep in zip(jan, reps):
            j = (t >= ini) & (t <= fim)
            if j.sum() < 5 or rep['duracao_s'] is None:
                continue
            tr = t[j]
            curva = interpolacao_linear(tau, (tr - tr[0]) / (tr[-1] - tr[0]), exc[j])
            # Trajetória ideal na amplitude que a repetição alcançou: o RMSE mede a FORMA e o TEMPO do movimento
            # (controle); a amplitude já é avaliada à parte (excursão e simetria entre braços)
            ideal = curva.min() + (curva.max() - curva.min()) * forma_ideal
            curvas.append(curva)
            ideais.append(ideal)
            erros_traj.append(rmse(curva, ideal))
        if series_temporais:
            sinais = dict(t=t - t[0], theta=theta, theta_suave=media_movel(theta), a_lin=a_lin, a_lenta=a_lenta,
                          tremor=a_lin - a_lenta, jerk=np.concatenate([[0.0], jerk]))
    if not curvas:
        return None

    tremor = np.concatenate(residuos)
    curva_media = np.mean(curvas, axis=0)
    metricas = dict(duracao_s=duracao, i_suavidade=i_suavidade, jerk_medio=i_suavidade / duracao,
                    rms_tremor=np.sqrt(np.mean(tremor ** 2)),
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
# 3b. EXPORTAÇÃO — tudo o que foi coletado de um paciente em um .zip (CSV + gráficos PNG)
#     CSV no padrão do Excel em português: separador ";" e vírgula decimal, UTF-8 com BOM
# =====================================================================
CORES = {'saudavel': '#2a78d6', 'afetado': '#eb6834', 'neutro': '#9a9993', 'faixa': '#0ca30c22'}
NOME_BRACO = {'saudavel': 'Saudável', 'afetado': 'Afetado'}


def slug(texto):
    """Nome seguro para arquivo: sem acentos, minúsculo, com _ no lugar de espaços."""
    texto = unicodedata.normalize('NFKD', str(texto)).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '_', texto.lower()).strip('_') or 'sem_nome'


def celula(v):
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'sim' if v else 'não'
    if isinstance(v, float):
        return '' if not np.isfinite(v) else f'{v:.6g}'.replace('.', ',')
    return str(v)


def csv_bytes(cabecalho, linhas):
    saida = io.StringIO()
    escritor = csv.writer(saida, delimiter=';', lineterminator='\r\n')
    escritor.writerow(cabecalho)
    escritor.writerows([celula(v) for v in linha] for linha in linhas)
    return saida.getvalue().encode('utf-8-sig')


def png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=130, bbox_inches='tight')
    return buf.getvalue()


def figura(titulo, linhas=2, colunas=2):
    from matplotlib.figure import Figure        # API orientada a objetos: segura com o servidor em várias threads
    fig = Figure(figsize=(6.2 * colunas, 3.9 * linhas), layout='constrained')
    eixos = fig.subplots(linhas, colunas, squeeze=False).ravel()
    fig.suptitle(titulo, fontsize=14, fontweight='bold')
    for ax in eixos:
        ax.grid(axis='y', color='#e4e3df'); ax.spines[['top', 'right']].set_visible(False)
    return fig, eixos


def sem_dados(ax, titulo):
    ax.set_title(titulo, loc='left', fontsize=11)
    ax.text(0.5, 0.5, 'sem dados suficientes', ha='center', va='center', color=CORES['neutro'], transform=ax.transAxes)
    ax.set_xticks([]); ax.set_yticks([])


def nan(v):
    return np.nan if v is None else float(v)


def grafico_evolucao(exercicio, sessoes):
    """Evolução de um exercício ao longo das sessões concluídas."""
    rotulos = [f"{i + 1}ª\n{datetime.fromisoformat(s['inicio']):%d/%m}" for i, s in enumerate(sessoes)]
    x = np.arange(len(sessoes))
    b = lambda s, braco: s['analise']['bracos'].get(braco) or {}
    fig, eixos = figura(f'Evolução — {exercicio}')
    paineis = [
        ('Score geral (0 a 100)', lambda s, k: b(s, k).get('score_geral'), (0, 100)),
        ('Repetições no ritmo (%)', lambda s, k: None if b(s, k).get('jogo', {}).get('aderencia_cadencia') is None
         else 100 * b(s, k)['jogo']['aderencia_cadencia'], (0, 100)),
        ('Desvio do movimento ideal (°)', lambda s, k: b(s, k).get('metricas', {}).get('rmse_trajetoria'), (0, None)),
    ]
    for ax, (titulo, f, lim) in zip(eixos, paineis):
        ax.set_title(titulo, loc='left', fontsize=11)
        for braco in BRACOS:
            ax.plot(x, [nan(f(s, braco)) for s in sessoes], 'o-', color=CORES[braco], label=NOME_BRACO[braco], lw=2)
        if lim:
            ax.set_ylim(*lim)
        ax.set_xticks(x, rotulos, fontsize=8); ax.legend(fontsize=8, frameon=False)
    ax = eixos[3]
    amp = [nan((s['analise'].get('simetria') or {}).get('excursao_pct')) for s in sessoes]
    ax.set_title('Amplitude do afetado (% do saudável)', loc='left', fontsize=11)
    ax.axhspan(90, 110, color=CORES['faixa'], label='faixa de simetria')
    ax.axhline(100, color=CORES['neutro'], ls='--', lw=1)
    ax.plot(x, amp, 'o-', color=CORES['afetado'], lw=2.5)
    ax.set_ylim(0, max([110] + [v for v in amp if np.isfinite(v)]) * 1.05)
    ax.set_xticks(x, rotulos, fontsize=8); ax.legend(fontsize=8, frameon=False)
    return png(fig)


def grafico_sessao(sessao, series, reps):
    """Resumo visual de uma sessão: repetição média, comparação entre braços e cada repetição."""
    an = sessao['analise'] or {}
    bracos = an.get('bracos', {})
    fig, eixos = figura(f"{sessao['exercicio']} — {datetime.fromisoformat(sessao['inicio']):%d/%m/%Y %H:%M}"
                        + (f" · score do afetado {sessao['score_geral']:.0f}" if sessao['score_geral'] is not None else ''))

    ax = eixos[0]
    if any('curva_media' in bracos.get(k, {}) for k in BRACOS):
        ax.set_title('Repetição média (ângulo a partir da posição baixa)', loc='left', fontsize=11)
        for k in BRACOS:
            if 'curva_media' in bracos.get(k, {}):
                c = np.array(bracos[k]['curva_media'], float)
                ax.plot(np.linspace(0, 100, len(c)), c, color=CORES[k], lw=2.5, label=NOME_BRACO[k])
        ideal = (bracos.get('afetado') or {}).get('curva_ideal')
        if ideal:
            ax.plot(np.linspace(0, 100, len(ideal)), ideal, '--', color=CORES['neutro'], label='Ideal (afetado)')
        ax.set_xlabel('início → fim da repetição (%)'); ax.set_ylabel('°'); ax.legend(fontsize=8, frameon=False)
    else:
        sem_dados(eixos[0], 'Repetição média')

    ax = eixos[1]
    aspectos = [('Amplitude\n(% da meta)', lambda r: min(100, r['metricas']['amplitude_pct']) if r.get('metricas', {}).get('amplitude_pct') is not None else None),
                ('Ritmo\n(% no tempo)', lambda r: None if (r.get('jogo') or {}).get('aderencia_cadencia') is None else 100 * r['jogo']['aderencia_cadencia']),
                ('Controle', lambda r: (r.get('notas') or {}).get('trajetoria')),
                ('Fluidez', lambda r: (r.get('notas') or {}).get('suavidade'))]
    if any(bracos.get(k, {}).get('metricas') for k in BRACOS):
        ax.set_title('Afetado × saudável (0 a 100, maior é melhor)', loc='left', fontsize=11)
        y = np.arange(len(aspectos))
        for i, k in enumerate(BRACOS):
            vals = [nan(f(bracos.get(k) or {})) for _, f in aspectos]
            ax.barh(y + (i - 0.5) * 0.38, vals, height=0.36, color=CORES[k], label=NOME_BRACO[k])
        ax.set_yticks(y, [a for a, _ in aspectos], fontsize=8); ax.invert_yaxis(); ax.set_xlim(0, 100)
        ax.grid(axis='x', color='#e4e3df'); ax.grid(axis='y', visible=False); ax.legend(fontsize=8, frameon=False)
    else:
        sem_dados(ax, 'Afetado × saudável')

    serie_de = {s['id']: s for s in series}
    reps = [r for r in reps if r['serie_id'] in serie_de]
    if reps:
        x = np.arange(len(reps))
        rot = [f"S{serie_de[r['serie_id']]['n']}·{r['n']}" for r in reps]
        cores = [CORES[serie_de[r['serie_id']]['braco']] for r in reps]
        meta = lambda r: abs(serie_de[r['serie_id']]['amp_max'] - serie_de[r['serie_id']]['amp_min'])
        ax = eixos[2]
        ax.set_title('Amplitude de cada repetição (% da meta)', loc='left', fontsize=11)
        ax.bar(x, [100 * r['amplitude'] / meta(r) if meta(r) else np.nan for r in reps], color=cores)
        ax.axhline(100, color=CORES['neutro'], ls='--', lw=1)
        ax = eixos[3]
        ax.set_title('Tempo de cada repetição (% do tempo pedido)', loc='left', fontsize=11)
        ax.axhspan(80, 120, color=CORES['faixa'])
        ax.scatter(x, [np.nan if r['duracao_s'] is None else 100 * r['duracao_s'] * r['cadencia'] / 60 for r in reps], c=cores, s=22)
        passo = max(1, len(reps) // 20)
        for ax in eixos[2:]:
            ax.set_xticks(x[::passo], rot[::passo], fontsize=7, rotation=45)
            ax.legend(handles=[Patch(color=CORES[k], label=NOME_BRACO[k]) for k in BRACOS], fontsize=8, frameon=False)
    else:
        sem_dados(eixos[2], 'Amplitude de cada repetição'); sem_dados(eixos[3], 'Tempo de cada repetição')
    return png(fig)


def exportar_paciente(pid):
    """Monta o .zip com CSVs (paciente, sessões, séries, repetições, leituras do sensor) e gráficos PNG."""
    p = consulta('SELECT * FROM patients WHERE id = ?', pid)[0]
    sessoes = consulta(SQL_SESSAO + 'WHERE s.patient_id = ? ORDER BY s.inicio', pid)
    for s in sessoes:
        s['exercicio'] = s['exercicio_nome'] or s['exercicio']
        s['analise'] = json.loads(s['analise']) if s['analise'] else None
    series = consulta('SELECT se.* FROM series se JOIN sessions s ON s.id = se.session_id WHERE s.patient_id = ? '
                      'ORDER BY se.session_id, se.n, se.inicio', pid)
    reps = consulta('SELECT r.* FROM reps r JOIN sessions s ON s.id = r.session_id WHERE s.patient_id = ? '
                    'ORDER BY r.session_id, r.serie_id, r.n', pid)
    serie_de = {s['id']: s for s in series}
    sessao_de = {s['id']: s for s in sessoes}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('LEIA-ME.txt', LEIA_ME.format(nome=p['nome'], data=datetime.now().strftime('%d/%m/%Y %H:%M')).encode('utf-8-sig'))
        j = (consulta('SELECT xp, conquistas FROM jogadores WHERE patient_id = ?', pid) or [{'xp': 0, 'conquistas': None}])[0]
        z.writestr('paciente.csv', csv_bytes(['id', 'nome', 'nascimento', 'observacoes', 'total_sessoes', 'xp_jogo', 'conquistas_jogo'],
                                             [[p['id'], p['nome'], p['nascimento'], p['observacoes'], len(sessoes), j['xp'] or 0,
                                               ', '.join(json.loads(j['conquistas'] or '{}'))]]))

        # Sessões: configuração + resumo de cada braço + comparação entre braços
        por_braco = [('total_reps', 'jogo'), ('aderencia_cadencia', 'jogo'), ('tempo_relativo', 'jogo'), ('amp_media', 'jogo'),
                     ('amplitude_alvo', 'jogo'), ('pontuacao', 'jogo'), ('score_geral', None), ('excursao', 'metricas'),
                     ('amplitude_pct', 'metricas'), ('rmse_trajetoria', 'metricas'), ('jerk_medio', 'metricas'),
                     ('i_suavidade', 'metricas'), ('rms_tremor', 'metricas'),
                     ('vel_linear_pico', 'metricas'), ('trajetoria', 'notas'), ('simetria', 'notas'),
                     ('suavidade', 'notas')]
        cab = ['sessao_id', 'inicio', 'fim', 'exercicio', 'series_alvo', 'reps_alvo', 'ordem', 'cadencia_alvo', 'amp_min',
               'amp_max', 'duracao_min', 'score_geral_afetado', 'simetria_rmse', 'simetria_excursao_pct', 'simetria_atraso_apice_pct']
        cab += [braco + '_' + ('nota_' if grupo == 'notas' else '') + campo for braco in BRACOS for campo, grupo in por_braco]
        linhas = []
        for s in sessoes:
            an = s['analise'] or {}
            sim = an.get('simetria') or {}
            linha = [s['id'], s['inicio'], s['fim'], s['exercicio'], s['series_alvo'], s['reps_alvo'], s['ordem'],
                     s['cadencia_alvo'], s['amp_min'], s['amp_max'], s['duracao_min'], s['score_geral'],
                     sim.get('rmse'), sim.get('excursao_pct'), sim.get('atraso_apice_pct')]
            for braco in BRACOS:
                r = (an.get('bracos') or {}).get(braco) or {}
                linha += [r.get(campo) if grupo is None else (r.get(grupo) or {}).get(campo) for campo, grupo in por_braco]
            linhas.append(linha)
        z.writestr('sessoes.csv', csv_bytes(cab, linhas))

        cab = ['serie_id', 'sessao_id', 'exercicio', 'n', 'braco', 'inicio', 'fim', 'cadencia', 'amp_min', 'amp_max', 'duracao_s',
               'total_reps', 'amp_media', 'vel_media', 'aderencia_cadencia', 'obstaculos', 'pontuacao', 'score_geral']
        z.writestr('series.csv', csv_bytes(cab, [[s['id'], s['session_id'], sessao_de[s['session_id']]['exercicio']]
                                                 + [s[c] for c in cab[3:]] for s in series]))

        cab = ['sessao_id', 'serie_id', 'serie_n', 'braco', 'rep_n', 't_ms', 'amplitude', 'vel_pico', 'duracao_s', 'no_ritmo', 'cadencia']
        z.writestr('repeticoes.csv', csv_bytes(cab, [
            [r['session_id'], r['serie_id'], (serie_de.get(r['serie_id']) or {}).get('n'), (serie_de.get(r['serie_id']) or {}).get('braco'),
             r['n'], r['t_ms'], r['amplitude'], r['vel_pico'], r['duracao_s'], None if r['no_ritmo'] is None else bool(r['no_ritmo']),
             r['cadencia']] for r in reps]))

        # Leituras do sensor de cada sessão (pacote do ESP32 a 50 Hz)
        for s in sessoes:
            d = ler_brutos(s['id'])
            if d is not None:
                z.writestr(f"leituras_sensor/sessao_{s['id']}_{s['inicio'][:10]}.csv",
                           csv_bytes(['t_ms', 'theta_graus', 'ax_m_s2', 'ay_lin_m_s2', 'az_lin_m_s2'],
                                     [[int(l[0])] + [float(v) for v in l[1:5]] for l in d]))

        # Gráficos: evolução por exercício e resumo de cada sessão concluída
        feitas = [s for s in sessoes if s['fim'] and s['analise']]
        for nome in dict.fromkeys(s['exercicio'] for s in feitas):
            do_ex = [s for s in feitas if s['exercicio'] == nome]
            z.writestr(f'graficos/evolucao_{slug(nome)}.png', grafico_evolucao(nome, do_ex))
        for s in feitas:
            z.writestr(f"graficos/sessoes/sessao_{s['id']}_{s['inicio'][:10]}_{slug(s['exercicio'])}.png",
                       grafico_sessao(s, [x for x in series if x['session_id'] == s['id']],
                                      [r for r in reps if r['session_id'] == s['id']]))
    buf.seek(0)
    return buf, f"halter_{slug(p['nome'])}_{datetime.now():%Y-%m-%d}.zip"


LEIA_ME = '''Halter Inteligente — dados exportados
Paciente: {nome}
Exportado em: {data}

Os arquivos .csv usam ponto e vírgula (;) como separador e vírgula decimal, e abrem direto no Excel.

paciente.csv          cadastro do paciente
sessoes.csv           uma linha por sessão: configuração, score e indicadores de cada braço
                      (saudavel_* e afetado_*) e a comparação entre os braços (simetria_*)
series.csv            uma linha por série (cada série é feita com um braço)
repeticoes.csv        uma linha por repetição detectada pelo jogo
leituras_sensor/      leituras do haltere a 50 Hz: tempo (ms), ângulo θ (°) e aceleração (m/s²),
                      com a gravidade já subtraída nos três eixos
graficos/             evolução de cada exercício e, em graficos/sessoes/, o resumo de cada sessão

Unidades principais
  amplitude, excursao, rmse_*      graus (°)
  aderencia_cadencia               fração das repetições no ritmo (0 a 1)
  tempo_relativo                   duração média da repetição ÷ tempo pedido (1 = no ritmo)
  jerk_medio                       m/s³          rms_tremor     m/s²
  vel_linear_pico                  m/s           score e notas  0 a 100
'''


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
    db().execute('DELETE FROM jogadores WHERE patient_id = ?', (pid,))
    db().execute('DELETE FROM patients WHERE id = ?', (pid,))
    db().commit()
    return jsonify(ok=True, sessoes_excluidas=len(sessoes))


@app.get('/api/patients/<int:pid>/export')
@login_obrigatorio
def exportar(pid):
    """Baixa um .zip com tudo o que foi coletado do paciente (CSV + gráficos)."""
    if not consulta('SELECT id FROM patients WHERE id = ?', pid):
        abort(404)
    arquivo, nome = exportar_paciente(pid)
    return send_file(arquivo, mimetype='application/zip', as_attachment=True, download_name=nome)


@app.get('/api/exercicios')
@login_obrigatorio
def listar_exercicios():
    return jsonify(consulta('SELECT e.*, COUNT(s.id) AS sessoes FROM exercicios e LEFT JOIN sessions s '
                            'ON s.exercicio_id = e.id GROUP BY e.id ORDER BY e.padrao DESC, e.nome'))


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
    exercicio = consulta('SELECT id, nome FROM exercicios WHERE id = ? AND padrao = 1', int(d['exercicio_id']))
    if not exercicio:
        falha(404, 'Exercício não disponível.')
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


# Progresso do paciente no jogo (nível, conquistas, personagem e cenário), acessado pela sessão em andamento
NOME_VALIDO = re.compile(r'^[a-z0-9_#]{1,40}$')


def paciente_da_sessao(sid):
    linha = consulta('SELECT patient_id FROM sessions WHERE id = ?', sid)
    if not linha:
        abort(404)
    return linha[0]['patient_id']


@app.get('/api/sessions/<int:sid>/jogador')
@login_obrigatorio
def ler_jogador(sid):
    """Progresso salvo + estatísticas das sessões anteriores (para conquistas acumuladas)."""
    pid = paciente_da_sessao(sid)
    j = (consulta('SELECT * FROM jogadores WHERE patient_id = ?', pid) or [{}])[0]
    reps = consulta('SELECT COUNT(*) AS n FROM reps r JOIN sessions s ON s.id = r.session_id '
                    'WHERE s.patient_id = ? AND s.id != ?', pid, sid)[0]['n']
    sessoes = consulta('SELECT COUNT(*) AS n FROM sessions WHERE patient_id = ? AND fim IS NOT NULL AND id != ?', pid, sid)[0]['n']
    nome = consulta('SELECT nome FROM patients WHERE id = ?', pid)[0]['nome']
    return jsonify(nome=nome, xp=j.get('xp') or 0, conquistas=json.loads(j.get('conquistas') or '{}'),
                   visual=json.loads(j.get('visual') or '{}'), estatisticas={'reps_total': reps, 'sessoes': sessoes})


@app.put('/api/sessions/<int:sid>/jogador')
@login_obrigatorio
def salvar_jogador(sid):
    pid = paciente_da_sessao(sid)
    d = request.get_json(force=True)
    xp = int(d['xp'])
    if not 0 <= xp <= 10_000_000:
        raise ValueError('xp fora da faixa')
    conquistas = {str(k): str(v)[:40] for k, v in dict(d.get('conquistas') or {}).items()}
    visual = {str(k): str(v) for k, v in dict(d.get('visual') or {}).items()
              if k in ('personagem', 'cor', 'acessorio', 'tema')}
    if len(conquistas) > 100 or not all(NOME_VALIDO.match(x) for x in [*conquistas, *visual.values()]):
        raise ValueError('conquista ou item de personalização inválido')
    db().execute('INSERT INTO jogadores (patient_id, xp, conquistas, visual, atualizado) VALUES (?, ?, ?, ?, ?) '
                 'ON CONFLICT(patient_id) DO UPDATE SET xp = excluded.xp, conquistas = excluded.conquistas, '
                 'visual = excluded.visual, atualizado = excluded.atualizado',
                 (pid, xp, json.dumps(conquistas), json.dumps(visual), datetime.now().isoformat(timespec='seconds')))
    db().commit()
    return jsonify(ok=True)


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
    """Uma série com as repetições e os sinais (θ, aceleração, tremor, jerk) para os gráficos."""
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
