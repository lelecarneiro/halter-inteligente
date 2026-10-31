# Halter Inteligente

Sistema de reabilitação para pacientes com hemiparesia após AVC. Um haltere com sensor de movimento transforma exercícios que já fazem parte da rotina de reabilitação em **dados objetivos** para o profissional de saúde e em um **jogo** que orienta e motiva o paciente.

Em cada sessão, o paciente faz séries com o **braço saudável** e com o **braço afetado**. O sistema compara os dois braços e acompanha a evolução ao longo do tratamento.

| Parte | O que faz | Arquivo |
|---|---|---|
| **Haltere (firmware)** | Lê o sensor, corrige os erros, calcula o ângulo e envia os dados por Bluetooth | `firmware/main.ino` |
| **Backend + módulo de análise** | Recebe os dados do haltere, repassa ao jogo em tempo real, guarda o histórico e calcula as métricas clínicas | `backend/app.py` |
| **Plataforma do profissional** | Pacientes, configuração das sessões, análise e evolução | `frontend/index.html` |
| **Jogo do paciente** | Estilo Flappy Bird: o pássaro acompanha o braço e os obstáculos marcam o ritmo | `frontend/game/index.html` |

---

## Sumário
1. [Como funciona](#como-funciona)
2. [Como rodar](#como-rodar)
3. [Passo a passo de uma sessão](#passo-a-passo-de-uma-sessão)
4. [Usando a plataforma](#usando-a-plataforma)
5. [Usando o jogo](#usando-o-jogo)
6. [Métodos numéricos (memorial de cálculo)](#métodos-numéricos-memorial-de-cálculo)
7. [Referência técnica](#referência-técnica)
8. [Problemas comuns](#problemas-comuns)
9. [Limitações conhecidas](#limitações-conhecidas)

---

## Como funciona

```
 ┌────────────────────┐   Bluetooth    ┌────────────────────────┐   tempo real (SSE)   ┌──────────────────┐
 │  ESP32 + MPU-6050  │ ─────────────▶ │     backend/app.py     │ ───────────────────▶ │  Jogo (paciente) │
 │  lê a 100 Hz       │   50 Hz        │  • repassa as leituras │                      │  /game/          │
 │  envia a 50 Hz     │                │  • grava dados brutos  │ ◀─────────────────── └──────────────────┘
 └────────────────────┘                │  • SQLite (histórico)  │   séries e repetições
                                       │  • módulo de análise   │
                                       │                        │ ◀──────────────────▶ ┌──────────────────┐
                                       └────────────────────────┘   API REST           │   Plataforma /   │
                                                                                       │  (profissional)  │
                                                                                       └──────────────────┘
```

### Como uma sessão é organizada

```
Sessão  (paciente + exercício + configuração)
 ├─ Série 1 ── braço saudável ── repetições
 ├─ Série 1 ── braço afetado  ── repetições
 ├─ Série 2 ── braço saudável ── repetições
 ├─ Série 2 ── braço afetado  ── repetições
 └─ …
```

- **Séries:** o profissional escolhe quantas, e **cada série é feita com os dois braços**, na ordem escolhida.
- **Métricas:** o sistema calcula as métricas de cada série, de cada braço (todas as séries dele juntas) e a **simetria** entre os dois braços da mesma sessão.
- **Score geral da sessão:** é o do **braço afetado**.

---

## Como rodar

### Pré-requisitos
- **Python 3.9 ou mais novo.** No Mac, use sempre o comando `python3`.
- **Chrome, Edge ou Safari** atualizado.
- **Internet** para carregar a biblioteca de gráficos (Chart.js). Sem internet, tudo funciona, mas os gráficos não aparecem.
- **Para o haltere:** ESP32, MPU-6050, dois LEDs e **Arduino IDE** com o pacote de placas ESP32 instalado.

### 1. Gravar o firmware no ESP32
1. Abra `firmware/main.ino` na Arduino IDE. Se ela pedir para criar uma pasta `main`, aceite.
2. Selecione a placa **ESP32 Dev Module** e a porta USB.
3. Clique em **Carregar**.
4. Com o haltere **parado sobre a mesa**, aguarde a calibração (cerca de 1 s):

| LED | Significado |
|---|---|
| Vermelho aceso | Calibrando: não mexa no haltere |
| Verde aceso | Pronto para uso |
| Vermelho piscando | Sensor não encontrado (confira os fios SDA/SCL) |

**Ligações:**

| Componente | ESP32 |
|---|---|
| MPU-6050 SDA | GPIO 21 |
| MPU-6050 SCL | GPIO 22 |
| MPU-6050 VCC | 3V3 |
| MPU-6050 GND | GND |
| LED verde | GPIO 19 |
| LED vermelho | GPIO 18 |

### 2. Instalar as dependências e iniciar o backend
Na pasta do projeto:

```bash
python3 -m pip install -r requirements.txt
python3 backend/app.py
```

O backend procura sozinho um dispositivo Bluetooth chamado **"Haltere"**, conecta e reconecta se o sinal cair. Deixe o Terminal aberto enquanto usar o sistema.

> 🍎 **No Mac:** na primeira execução, o sistema pede permissão de Bluetooth para o Terminal. Aceite. Se negar por engano, libere em **Ajustes do Sistema → Privacidade e Segurança → Bluetooth**.

### 3. Abrir a plataforma
Acesse **http://localhost:5000** e entre com o login padrão:

| Usuário | Senha |
|---|---|
| `fisio` | `halter123` |

Para trocar o login, defina variáveis de ambiente ao iniciar:

```bash
HALTER_USUARIO=seunome HALTER_SENHA=suasenha HALTER_SEGREDO=uma-frase-longa python3 backend/app.py
```

### Testar sem o haltere
No jogo, clique em **Modo simulado**. Um "paciente virtual" segue a trilha dos obstáculos e gera o mesmo pacote do ESP32 (ângulo e aceleração), que é enviado ao backend.

- **Análise completa:** a comparação entre os braços também funciona no modo simulado.
- **Braço afetado simulado:** tem menos amplitude, atraso, mais tremor e mais compensação lateral, para a comparação ficar visível.
- **Jogo sem a plataforma:** também pode ser aberto sozinho em **http://localhost:5000/game/**. Nesse caso, nada é salvo no histórico.

---

## Passo a passo de uma sessão

1. **Plataforma → Pacientes:** escolha o paciente (ou cadastre um novo).
2. **Nova sessão:** escolha o exercício, o número de séries, as repetições por série, qual braço começa, a cadência, a amplitude e a duração máxima de cada série. Clique em **Criar sessão e abrir o jogo**.
3. **No jogo:** clique em **Conectar haltere** (ou **Modo simulado**). O painel mostra a primeira etapa, por exemplo "Série 1 · Braço saudável".
4. **Ajuste a amplitude do braço da vez:** peça ao paciente para ir até a posição baixa e toque em **"atual"**, e depois faça o mesmo na posição alta.
5. Toque em **Iniciar série**. A série termina sozinha ao atingir as repetições ou a duração máxima, ou quando você toca em **Encerrar série**.
6. **Intervalo:** aparece o resultado da série e o aviso **"Passe o haltere para a outra mão"**. Ajuste a amplitude do outro braço (passo 4) e toque em **Começar próxima série**.
7. Repita até a última série. Para parar antes, use **Encerrar sessão agora** no intervalo.
8. **Resumo final:** aparecem a pontuação total, as estrelas e um cartão por braço. Toque em **Ver análise na plataforma**.

---

## Usando a plataforma

| Tela | O que tem |
|---|---|
| **Pacientes** | Lista de pacientes e cadastro (nome, nascimento, observações clínicas) |
| **Paciente** | Editar ou excluir · nova sessão · evolução dos dois braços · histórico (tudo filtrado por exercício) |
| **Sessão** | Score e notas do braço afetado · comparação entre os braços · tabela de séries · sinais de cada série |

### Nova sessão

| Campo | Uso |
|---|---|
| Exercício | Categoria do movimento (veja a lista abaixo) |
| Nº de séries | 1 a 10; cada série é feita com os dois braços |
| Repetições por série | Meta de cada série (em branco = livre, termina pela duração) |
| Cada série começa pelo braço | Saudável → afetado, ou afetado → saudável |
| Cadência | Repetições por minuto (4 a 30) |
| Duração máx. | Limite de tempo de cada série, em minutos |
| Posição baixa / alta | Amplitude inicial, que pode ser ajustada por braço no jogo |

Ao escolher um exercício, os campos são preenchidos com os valores da **última sessão daquele exercício** do paciente. Abaixo do botão aparece o plano, por exemplo: "3 séries × 2 braços = 6 etapas".

### Exercícios
Cada sessão pertence a uma **categoria de exercício**. A evolução, os gráficos e o histórico mostram **um exercício por vez**: o histórico de elevação lateral, por exemplo, só compara sessões de elevação lateral. Use o filtro **Exercício** para trocar.

| Categoria | Movimento |
|---|---|
| Flexão de cotovelo | Rosca bíceps: dobrar o cotovelo trazendo o haltere ao ombro |
| Extensão de cotovelo | Tríceps: estender o cotovelo, com o braço elevado ou apoiado |
| Elevação lateral | Abdução de ombro: elevar o braço estendido para o lado |
| Elevação frontal | Flexão de ombro: elevar o braço estendido à frente |
| Desenvolvimento de ombro | Elevar o haltere da altura do ombro até acima da cabeça |
| Remada | Puxar o haltere em direção ao tronco |
| Rotação externa de ombro | Cotovelo junto ao corpo a 90°: girar o antebraço para fora |
| Flexão de punho | Antebraço apoiado, palma para cima: dobrar o punho |
| Extensão de punho | Antebraço apoiado, palma para baixo: estender o punho |
| Pronação e supinação | Girar o antebraço, virando a palma para cima e para baixo |

**Criar uma categoria:** na lista **Exercício**, escolha **"+ Nova categoria…"** e informe o nome e, se quiser, a descrição. Categorias criadas por você podem ser excluídas enquanto ainda não tiverem sessões.

### Evolução do paciente
- **Score geral por braço:** uma linha para o saudável (azul) e outra para o afetado (laranja).
- **Amplitude do afetado em % do saudável:** quanto mais perto de 100 %, mais simétricos os braços.
- **6 gráficos de métricas, com uma linha por braço:** amplitude média, repetições no ritmo, erro de trajetória, tremor, jerk médio e compensação lateral.
- **Histórico:** tabela das sessões com os valores dos dois braços lado a lado.

### Análise da sessão
- **Score geral (braço afetado)** e as 4 notas: trajetória, simetria, estabilidade e suavidade.
- **Comparação entre os braços:**
  - **Indicadores:** amplitude do afetado em % do saudável, diferença entre as trajetórias (RMSE), atraso do ápice e score do afetado em % do saudável.
  - **Trajetória média por repetição:** as curvas dos dois braços sobrepostas, com a trajetória ideal.
  - **Notas por braço:** barras lado a lado.
  - **Amplitude e score por série:** uma linha por braço, para ver fadiga ou melhora durante a sessão.
  - **Métricas lado a lado:** todas as métricas do memorial para os dois braços, com o afetado em % do saudável.
- **Séries:** tabela com repetições, ritmo, amplitude, obstáculos, pontos e score de cada série.
- **Sinais de uma série:** ângulo bruto e suavizado, trajetória vs. ideal, aceleração, tremor, jerk, deslocamento lateral, amplitude e duração de cada repetição.

### Editar e excluir pacientes
Na página do paciente, use **Editar** para corrigir nome, nascimento e observações. **Excluir paciente** pede confirmação e apaga também todas as sessões, séries, repetições e leituras brutas do haltere. A exclusão não pode ser desfeita.

---

## Usando o jogo

### Para o paciente
- O **pássaro** sobe e desce acompanhando o ângulo do haltere.
- Os **canos** chegam no ritmo da cadência, alternando abertura no alto (levantar) e embaixo (abaixar). A **linha tracejada** mostra o movimento ideal.
- Cada obstáculo superado vale **+10 pontos**, e cada repetição no ritmo vale **+5**. O feedback aparece na hora: "No ritmo ✓", "Mais devagar" ou "Mais rápido".
- Bater no cano **não encerra o jogo**: o cano fica vermelho e não pontua.
- No fim, a **pontuação total** aparece em destaque, com até 3 estrelas (pela porcentagem de obstáculos superados) e um cartão por braço.

### Painel do profissional (lateral)

| Controle | Função |
|---|---|
| Conectar haltere / Modo simulado | Fonte dos dados |
| Séries da sessão | Lista das etapas, com a atual destacada e as concluídas riscadas |
| Cadência alvo | Muda o ritmo dos canos **na hora**, inclusive durante a série |
| Posição baixa / alta + "atual" | Amplitude do **braço da vez**. Cada braço guarda a sua |
| Duração máxima | Limite de tempo da série |
| Iniciar série / Encerrar série | Controle de cada etapa |

Se o sinal do haltere cair, o jogo **pausa sozinho** e retoma quando os dados voltam.

---

## Métodos numéricos (memorial de cálculo)

### No ESP32 (`firmware/main.ino`)

| Seção | Método |
|---|---|
| 5.1 | Leitura a 100 Hz (Δt = 0,01 s), envio a 50 Hz (Δt = 0,02 s) |
| 5.2 | Calibração: média de 500 amostras em repouso para o offset do giroscópio; conversão de unidades (8192 LSB/g, 65,5 LSB/(°/s)) |
| 5.4 | Ângulo: extrapolação de Euler `θ_prev = θ + ω·Δt`, ângulo do acelerômetro `atan2(ay, az)` e correção por gradiente descendente |
| 5.4.3 | Subtração da gravidade nos eixos Y e Z |

### No jogo (`frontend/game/index.html`)

| Cálculo | Método |
|---|---|
| Suavização da curva | Média móvel das últimas K = 10 amostras (5.7) |
| Velocidade angular | Diferenças finitas `(θᵢ − θᵢ₋₁) / Δt` |
| Repetições | Histerese: passou de 70 % da amplitude e voltou abaixo de 30 % |
| Ritmo | Repetição "no ritmo" se o período fica a até ±20 % de `60 / cadência` |

### No módulo de análise (`backend/app.py`)
Calculado para cada série e para cada braço (todas as séries dele juntas).

| Seção | Cálculo | Resultado |
|---|---|---|
| 5.5 Trapézios | Velocidade e posição a partir da aceleração, repetição a repetição | Velocidade linear de pico, deslocamento lateral |
| 5.6 Diferenças finitas | Jerk `J = Δa_lin / Δt`; índice de suavidade = trapézios de \|J\| | Jerk médio, **nota de suavidade** |
| 5.7 Média móvel e resíduo | K = 10; tremor = `a_lin − a_lenta`; intensidade pelo RMS | RMS do tremor |
| 5.8 Estatística | Média μx, variância S²x e desvio padrão Sx da posição lateral | **Nota de estabilidade** (compensação postural) |
| 5.9 Interpolação | Tempo normalizado `τ = t / T_total`, interpolação linear em 101 pontos, RMSE | **Nota de trajetória** (vs. ideal) e **nota de simetria** (afetado vs. saudável), atraso do ápice |
| 5.10 Score | `Nota = 100 · (1 − erro / erro_máx)`, limitada entre 0 e 100 | **Score geral** = 0,30·Trajetória + 0,30·Simetria + 0,25·Estabilidade + 0,15·Suavidade |

**Simetria.** O braço saudável é a referência do afetado **na própria sessão**. O score do braço afetado inclui a nota de simetria. O score do saudável não inclui, e o peso dela é redistribuído entre as outras notas.

**Observações de implementação:**
- **Integração dupla.** A integração dupla da aceleração acumula erro. Por isso, em cada repetição, o viés de cada eixo é removido e a velocidade é forçada a zero no início e no fim, quando o haltere está parado.
- **Trajetória.** A comparação de trajetórias usa o ângulo θ, que não acumula erro de integração. O ângulo é convertido em **excursão a partir da posição baixa**, no sentido da subida. Assim, os dois braços são comparáveis mesmo que o sensor fique invertido quando o haltere troca de mão.
- **Primeira repetição de cada série.** Ela parte do repouso e não tem um ciclo completo antes dela, por isso fica fora da comparação de trajetórias.
- **Limites de erro máximo.** Os limites que definem as notas ficam em `ERRO_MAX`, no início do módulo de análise. Os valores atuais são iniciais e **devem ser calibrados com voluntários saudáveis**:

| Métrica | Erro máximo atual |
|---|---|
| Trajetória (RMSE) | 15° |
| Simetria (RMSE) | 15° |
| Estabilidade (Sx) | 5 cm |
| Suavidade (jerk médio) | 40 m/s³ |

---

## Referência técnica

### Estrutura do repositório
```
firmware/main.ino          firmware do ESP32
backend/app.py             backend, banco de dados e módulo de análise
frontend/index.html        plataforma do profissional
frontend/game/index.html   jogo do paciente
requirements.txt           dependências Python
data/                      criada ao rodar (fora do Git)
  ├─ halter.db             banco SQLite
  └─ raw/sessao_<id>.csv   leituras brutas de cada sessão
```

### Banco de dados (SQLite)

| Tabela | Conteúdo |
|---|---|
| `patients` | Nome, nascimento, observações |
| `exercicios` | Categorias de exercício (padrão e personalizadas) |
| `sessions` | Paciente, exercício, configuração (séries, repetições, ordem, cadência, amplitude, duração), score geral e resumo da análise por braço |
| `series` | Número, braço, cadência e amplitude usadas, resultados do jogo e análise da série |
| `reps` | Cada repetição: tempo, amplitude, velocidade de pico, duração, se estava no ritmo |

O banco é **atualizado automaticamente** ao iniciar o backend. Sessões de versões anteriores, que tinham um braço só, viram sessões de uma série, sem perda de dados.

### Pacote enviado pelo haltere
Uma linha CSV a cada 20 ms, pelo Bluetooth (serviço UART da Nordic) e pela porta Serial (115200 baud):

```
t_ms,theta,ax,ay,az
12840,34.70,0.120,-0.310,0.215
```
`t_ms` é o tempo em ms, `theta` é o ângulo em graus e `ax, ay, az` é a aceleração linear em m/s².

### Parâmetros do jogo (URL)
A plataforma abre o jogo com a configuração na URL, por exemplo:

```
/game/?sessao=7&cadencia=12&min=0&max=60&duracao=2&reps=10&series=3&ordem=saudavel
```

### API do backend

| Método | Rota | Uso |
|---|---|---|
| `POST` | `/api/login` · `/api/logout` | Entrar e sair |
| `GET` / `POST` | `/api/patients` | Listar e cadastrar pacientes |
| `GET` · `PUT` · `DELETE` | `/api/patients/<id>` | Ver, editar ou excluir o paciente (exclui também as sessões) |
| `GET` | `/api/patients/<id>/sessions?exercicio_id=<id>` | Histórico com as métricas, opcionalmente de um exercício |
| `GET` / `POST` | `/api/exercicios` | Listar e criar categorias de exercício |
| `DELETE` | `/api/exercicios/<id>` | Excluir categoria personalizada ainda sem sessões |
| `POST` | `/api/sessions` | Criar sessão; devolve o link do jogo configurado |
| `POST` | `/api/sessions/<id>/series` | O jogo inicia uma série (número, braço, cadência e amplitude em uso) |
| `POST` | `/api/series/<id>/reps` | O jogo envia cada repetição |
| `POST` | `/api/series/<id>/end` | Encerrar a série; devolve o score dela |
| `POST` | `/api/sessions/<id>/brutos` | Leituras geradas pelo modo simulado |
| `POST` | `/api/sessions/<id>/end` | Encerrar a sessão; análise por braço e simetria |
| `GET` | `/api/sessions/<id>` | Sessão com séries, repetições e análise por braço |
| `GET` | `/api/series/<id>` | Série com os sinais para os gráficos |
| `GET` | `/api/stream` | Leituras do haltere em tempo real (Server-Sent Events) |

---

## Problemas comuns

| Problema | Solução |
|---|---|
| `zsh: command not found: python` | Use `python3` |
| `No matching distribution found for bleak` | Atualize o projeto (`git pull`). O `requirements.txt` usa uma versão compatível com Python 3.9 |
| `No module named 'bleak'` ou `'flask'` | Rode `python3 -m pip install -r requirements.txt` |
| Jogo mostra "procurando haltere…" | Confira se o ESP32 está ligado com o LED verde aceso e se o Terminal tem permissão de Bluetooth. Só um computador pode estar conectado ao haltere por vez |
| Jogo mostra "Sinal do haltere perdido" | O jogo pausa sozinho e retoma quando os dados voltam. Aproxime o haltere do computador |
| As repetições não são contadas | Ajuste a posição baixa e a alta do braço da vez com o botão **"atual"**. A repetição só conta se passar de 70 % da amplitude e voltar abaixo de 30 % |
| "Não foi possível registrar a série" | A sessão já foi encerrada. Crie uma nova sessão na plataforma |
| Análise sem métricas de tremor, jerk etc. | Não houve leituras do haltere durante a série. Confira a conexão, ou use o modo simulado para testar |
| Erro de banco depois de atualizar o projeto | O banco é atualizado automaticamente ao iniciar o backend. Se o erro continuar, apague `data/halter.db` (o histórico será perdido) |
| Gráficos não aparecem | Verifique a conexão com a internet (o Chart.js vem de um CDN) |
| Arduino IDE não compila | Instale o pacote de placas **esp32** da Espressif em Ferramentas → Placa → Gerenciador de placas |

---

## Limitações conhecidas
- O login é único e simples, adequado ao protótipo. Não use a senha padrão com dados reais de pacientes.
- O servidor aceita conexões apenas do próprio computador (`localhost`).
- O resíduo da média móvel (tremor) também captura um pouco do movimento voluntário, e o filtro de 12 Hz citado no memorial não foi implementado.
- O índice de suavidade (integral de |J| no tempo) tem unidade m/s².
- Os limites de erro máximo das notas (`ERRO_MAX`) ainda precisam ser calibrados com voluntários saudáveis.
