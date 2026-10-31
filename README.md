# Halter Inteligente

Sistema de reabilitação para pacientes com hemiparesia após AVC. Um haltere com sensor de movimento transforma exercícios que já fazem parte da rotina de reabilitação em **dados objetivos** para o profissional de saúde e em um **jogo** que orienta e motiva o paciente.

O sistema tem três partes integradas:

| Parte | O que faz | Arquivo |
|---|---|---|
| **Haltere (firmware)** | Lê o sensor, corrige os erros, calcula o ângulo e envia os dados por Bluetooth | `firmware/main.ino` |
| **Backend + módulo de análise** | Recebe os dados do haltere, repassa ao jogo em tempo real, guarda o histórico e calcula as métricas clínicas | `backend/app.py` |
| **Interfaces** | Plataforma do profissional (pacientes, sessões, gráficos) e jogo do paciente (estilo Flappy Bird) | `frontend/index.html` e `frontend/game/index.html` |

---

## Como funciona

```
 ┌────────────────────┐   Bluetooth    ┌────────────────────────┐   tempo real (SSE)   ┌──────────────────┐
 │  ESP32 + MPU-6050  │ ─────────────▶ │     backend/app.py     │ ───────────────────▶ │  Jogo (paciente) │
 │  lê a 100 Hz       │   50 Hz        │  • repassa as leituras │                      │  frontend/game/  │
 │  envia a 50 Hz     │                │  • grava dados brutos  │ ◀─────────────────── └──────────────────┘
 └────────────────────┘                │  • SQLite (histórico)  │   repetições e resumo
                                       │  • módulo de análise   │
                                       │                        │ ◀──────────────────▶ ┌──────────────────┐
                                       └────────────────────────┘   API REST           │   Plataforma     │
                                                                                       │  (profissional)  │
                                                                                       └──────────────────┘
```

### Fluxo de uma sessão
1. O profissional entra na **plataforma**, escolhe o paciente e configura a sessão: **exercício**, **número de séries**, repetições por série, qual braço começa, cadência, amplitude e duração máxima de cada série.
2. O **jogo** abre já configurado. **Cada série é feita com os dois braços**: por exemplo, com 3 séries começando pelo saudável, a sequência é Série 1 saudável → Série 1 afetado → Série 2 saudável → … (6 etapas). Entre as etapas aparece o resultado da série e o aviso para trocar o haltere de mão.
3. O paciente movimenta o haltere e o pássaro sobe e desce acompanhando o ângulo do braço.
4. Os canos chegam **no ritmo da cadência alvo**, alternando abertura no alto (levantar) e embaixo (abaixar). O profissional pode **ajustar a cadência ao vivo** pelo painel lateral.
5. Cada repetição é detectada, comparada com o ritmo alvo e enviada ao backend. O paciente recebe feedback ("No ritmo ✓", "Mais devagar", "Mais rápido") e pontos.
6. No fim, a **pontuação total** aparece em destaque, com estrelas e um cartão para cada braço (pontos, repetições, ritmo e score de movimento).
7. O **módulo de análise** aplica os métodos numéricos às leituras de cada série e de cada braço e **compara os dois braços da mesma sessão** (simetria).
8. O profissional vê a análise da sessão e a **evolução** do paciente ao longo do tratamento, sempre comparando sessões do **mesmo exercício**.

---

## Como rodar

### Pré-requisitos
- **Python 3.9 ou mais novo.** No Mac, use sempre o comando `python3`.
- **Chrome, Edge ou Safari** atualizado.
- **Internet** para carregar a biblioteca de gráficos (Chart.js). Sem internet, tudo funciona, mas os gráficos não aparecem.
- Para o haltere: ESP32, MPU-6050 e **Arduino IDE** com o pacote de placas ESP32 instalado.

### 1. Gravar o firmware no ESP32
1. Abra `firmware/main.ino` na Arduino IDE. Se ela pedir para criar uma pasta `main`, aceite.
2. Selecione a placa **ESP32 Dev Module** e a porta USB.
3. Clique em **Carregar**.
4. Com o haltere **parado sobre a mesa**, aguarde a calibração (cerca de 1 s):
   - LED vermelho aceso = calibrando
   - LED verde aceso = pronto
   - LED vermelho piscando = sensor não encontrado (confira os fios SDA/SCL)

**Ligações do MPU-6050:**

| MPU-6050 | ESP32 |
|---|---|
| SDA | GPIO 21 |
| SCL | GPIO 22 |
| VCC | 3V3 |
| GND | GND |

LEDs: verde no GPIO 19, vermelho no GPIO 18.

### 2. Instalar as dependências e iniciar o backend
Na pasta do projeto:

```bash
python3 -m pip install -r requirements.txt
python3 backend/app.py
```

O backend procura sozinho um dispositivo Bluetooth chamado **"Haltere"**, conecta e reconecta se o sinal cair.

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
Abra o jogo e clique em **Modo simulado**. Um "paciente virtual" segue a trilha dos canos e gera o mesmo pacote do ESP32 (ângulo e aceleração), que é enviado ao backend. Assim, **a análise completa e a comparação entre braços também funcionam no modo simulado**. O braço afetado é simulado com menos amplitude, atraso, mais tremor e mais compensação lateral, para a comparação ficar visível.

O jogo também pode ser aberto sozinho em **http://localhost:5000/game/**. Nesse caso, nada é salvo no histórico.

---

## Usando a plataforma

| Tela | O que tem |
|---|---|
| **Pacientes** | Lista de pacientes e cadastro (nome, nascimento, observações clínicas) |
| **Paciente** | Editar ou excluir o paciente · nova sessão · evolução do score dos dois braços e da simetria · 6 gráficos de métricas com uma linha por braço · histórico (tudo filtrado por exercício) |
| **Sessão** | Score geral e notas do braço afetado · **comparação entre os braços** · tabela de séries · sinais de cada série (ângulo, aceleração, tremor, jerk, deslocamento lateral, repetições) |

### Comparação entre os braços (página da sessão)
- **Indicadores:** amplitude do afetado em % do saudável, diferença entre as trajetórias (RMSE), atraso do ápice e score do afetado em % do saudável.
- **Trajetória média por repetição:** as curvas dos dois braços sobrepostas, com a trajetória ideal.
- **Notas por braço:** barras lado a lado para trajetória, estabilidade, suavidade e score geral.
- **Amplitude e score por série:** uma linha por braço, para ver fadiga ou melhora ao longo da sessão.
- **Métricas lado a lado:** todas as métricas do memorial para os dois braços, com o afetado em % do saudável.

### Exercícios e comparação entre sessões
Cada sessão pertence a uma **categoria de exercício**. A evolução, os gráficos e o histórico do paciente mostram **um exercício por vez**: o histórico de elevação lateral, por exemplo, só compara sessões de elevação lateral. Use o filtro **Exercício** acima dos gráficos para trocar.

Categorias já incluídas (membros superiores):

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

**Criar uma categoria:** no formulário de nova sessão, abra a lista **Exercício** e escolha **"+ Nova categoria…"**. Informe o nome e, se quiser, a descrição. Categorias criadas por você podem ser excluídas enquanto ainda não tiverem sessões.

Ao escolher um exercício, o formulário é preenchido com a cadência, a amplitude e as repetições da **última sessão daquele exercício** do paciente.

### Editar e excluir pacientes
Na página do paciente, use **Editar** para corrigir nome, nascimento e observações. **Excluir paciente** pede confirmação e apaga também todas as sessões, repetições e leituras brutas do haltere. A exclusão não pode ser desfeita.

**Simetria:** como cada sessão tem os dois braços, o braço saudável é a referência do afetado **na própria sessão**. O score geral da sessão é o do braço afetado, incluindo a nota de simetria. O braço saudável recebe um score sem simetria, e os pesos são redistribuídos entre as outras notas.

### Painel do jogo (ajustes ao vivo)
- **Cadência alvo** (4–30 rep/min): muda o ritmo dos canos na hora.
- **Posição baixa e alta:** peça ao paciente para ir até cada posição e toque em **"atual"**. **Cada braço guarda a sua amplitude**, porque o afetado costuma ter um alcance menor.
- **Séries da sessão:** lista com a etapa atual e as concluídas.
- **Iniciar série / Encerrar série.** Entre as séries, também é possível **encerrar a sessão** antes do fim.

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

| Seção | Cálculo | Resultado |
|---|---|---|
| 5.5 Trapézios | Velocidade e posição a partir da aceleração, repetição a repetição | Velocidade linear de pico, deslocamento lateral |
| 5.6 Diferenças finitas | Jerk `J = Δa_lin / Δt`; índice de suavidade = trapézios de \|J\| | Jerk médio, **nota de suavidade** |
| 5.7 Média móvel e resíduo | K = 10; tremor = `a_lin − a_lenta`; intensidade pelo RMS | RMS do tremor |
| 5.8 Estatística | Média μx, variância S²x e desvio padrão Sx da posição lateral | **Nota de estabilidade** (compensação postural) |
| 5.9 Interpolação | Tempo normalizado `τ = t / T_total`, interpolação linear em 101 pontos, RMSE | **Notas de trajetória e simetria**, atraso do ápice |
| 5.10 Score | `Nota = 100 · (1 − erro / erro_máx)`, limitada entre 0 e 100 | **Score geral** = 0,30·Trajetória + 0,30·Simetria + 0,25·Estabilidade + 0,15·Suavidade |

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

### Pacote enviado pelo haltere
Uma linha CSV a cada 20 ms, pelo Bluetooth (serviço UART da Nordic) e pela porta Serial (115200 baud):

```
t_ms,theta,ax,ay,az
12840,34.70,0.120,-0.310,0.215
```
`t_ms` é o tempo em ms, `theta` é o ângulo em graus e `ax, ay, az` é a aceleração linear em m/s².

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
| Erro de banco depois de atualizar o projeto | O banco é atualizado automaticamente ao iniciar o backend, sem perder dados. Se o erro continuar, apague `data/halter.db` (o histórico será perdido) |
| Gráficos não aparecem | Verifique a conexão com a internet (o Chart.js vem de um CDN) |
| Arduino IDE não compila | Instale o pacote de placas **esp32** da Espressif em Ferramentas → Placa → Gerenciador de placas |

---

## Limitações conhecidas
- O login é único e simples, adequado ao protótipo. Não use a senha padrão com dados reais de pacientes.
- O servidor aceita conexões apenas do próprio computador (`localhost`).
- O resíduo da média móvel (tremor) também captura um pouco do movimento voluntário, e o filtro de 12 Hz citado no memorial não foi implementado.
- O índice de suavidade (integral de |J| no tempo) tem unidade m/s².
