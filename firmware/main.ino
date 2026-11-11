/*
 * Halter Inteligente — firmware ESP32 + MPU-6050
 * Lê o IMU a 100 Hz, estima o ângulo θ e a altura vertical z do haltere e transmite a 50 Hz por BLE
 * (e Serial, para depuração).
 * Pacote CSV: t_ms,theta_graus,ax,ay,az,z_cm,parado
 *   ax, ay, az = aceleração linear em m/s² · z_cm = altura relativa (cm, positivo para cima)
 *   parado = 1 quando o haltere está em repouso (o jogo usa para corrigir a deriva da altura)
 *
 * A altura só pode mudar com o movimento VERTICAL. Girar o haltere não deve mexer nela, mas o acelerômetro
 * tem um erro de fábrica (viés, até ~0,8 m/s² por eixo) que, ao girar, vira uma falsa aceleração vertical.
 * Por isso o firmware aprende esse viés sozinho (ajuste de esfera por mínimos quadrados) e o guarda na memória:
 * na PRIMEIRA vez, com o LED verde piscando, deixe o haltere parado ~1 s com cada uma das 6 faces para cima.
 * Quando o LED verde ficar aceso direto, está calibrado. Para refazer, envie R pelo Monitor Serial.
 */
#include <Wire.h>
#include <BLEDevice.h>
#include <BLE2902.h>
#include <Preferences.h>

// ---------- Pinos e constantes ----------
#define MPU 0x68
#define LED_VERDE 19
#define LED_VERMELHO 18
const float ESC_ACEL = 8192.0;  // LSB/g  (faixa ±4 g)
const float ESC_GIRO = 65.5;    // LSB/(°/s) (faixa ±500 °/s)
const float G = 9.81;
const float BETA = 0.1;         // passo da descida do gradiente

// ---------- Altura: detecção de repouso (valores testados em simulação; ajuste com o haltere real) ----------
const int   JANELA = 17;             // média móvel de 0,17 s (≈ 1 ciclo de tremor de 6 Hz, que assim se cancela)
const float REPOUSO_ACEL = 0.05;     // m/s²  — média de (|a| − g) abaixo disso: sem aceleração linear
const float APRENDE = 0.005;         // em repouso firme, quanto g e o desvio do giroscópio se ajustam por leitura
const int   FIRME_MIN = 150;         // repouso firme por 1,5 s antes de aprender (não aprende nas pausas curtas entre repetições)
const float REPOUSO_GIRO = 5.0;      // °/s   — média da rotação abaixo disso
const float REPOUSO_VEL  = 0.10;     // m/s   — velocidade estimada abaixo disso
const int   REPOUSO_CURTO = 10;      // 0,1 s quieto e com velocidade baixa → parado
const int   REPOUSO_LONGO = 150;     // 1,5 s quieto → parado, qualquer que seja a velocidade estimada
const float MOV_MAX = 3.0;           // s sem repouso: a partir daí a velocidade é amortecida (limita a deriva)
const float TAU_FUGA = 2.0;          // s — constante de tempo desse amortecimento

// ---------- Calibração do viés do acelerômetro ----------
const int   MAX_POSES = 12;          // poses de repouso guardadas para o ajuste
const int   MIN_POSES = 6;           // mínimo para ajustar (ex.: as 6 faces)
const float POSE_DIST = 0.906;       // cos 25°: poses mais próximas que isso contam como a mesma
const float FIRME_GIRO = 3.0;        // °/s  — repouso firme: quase sem rotação...
const float FIRME_ACEL = 0.25;       // m/s² — ...e |a| quase constante na janela (não depende do viés)

// ---------- BLE (serviço UART da Nordic) ----------
BLECharacteristic *tx;
bool conectado = false;
struct Callbacks : BLEServerCallbacks {
  void onConnect(BLEServer *s) { conectado = true; }
  void onDisconnect(BLEServer *s) { conectado = false; BLEDevice::startAdvertising(); }
};

// ---------- Estado ----------
float offGiro[3] = {0, 0, 0}, theta = 0;
float grav[3];                 // direção da gravidade no referencial do sensor (m/s²)
float gLocal = G;              // módulo da gravidade medido na calibração (absorve erro de escala)
float vz = 0, z = 0;           // velocidade (m/s) e altura (m) verticais
float viesZ = 0;               // viés da aceleração vertical, aprendido em repouso
float tMov = 0;                // tempo desde o último repouso (s)
int nQuieto = 0, iJanela = 0, nFirme = 0;
float bufA[JANELA], bufM[JANELA], bufW[JANELA][3];   // janelas da média móvel (zeradas: variáveis globais)
Preferences memoria;
bool calibrado = false;
float viesA[3] = {0, 0, 0};    // viés do acelerômetro (m/s², eixos do sensor)
float poses[MAX_POSES][3];     // média de |a| bruta em cada pose de repouso
int nPoses = 0, proxPose = 0;
float somaPose[3] = {0, 0, 0};
int nPose = 0;
bool parado = true;
unsigned long tAnterior;
int contador = 0;

// Mensagem de estado ('#...'): vai pela Serial e, se conectado, pelo Bluetooth (o jogo mostra na tela)
void avisa(const char *msg) {
  Serial.println(msg);
  if (conectado) { tx->setValue((uint8_t *)msg, strlen(msg)); tx->notify(); }
}

// ---------- Funções auxiliares de I2C ----------
void escreve(byte reg, byte val) {
  Wire.beginTransmission(MPU); Wire.write(reg); Wire.write(val); Wire.endTransmission();
}
bool le(byte reg, int n, int16_t *dest) {  // lê n palavras de 16 bits a partir de reg; false se a leitura falhou
  Wire.beginTransmission(MPU); Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MPU, n * 2) != n * 2) return false;
  for (int i = 0; i < n; i++) { int alto = Wire.read(); dest[i] = (alto << 8) | Wire.read(); }
  return true;
}
bool sensorConectado() {
  Wire.beginTransmission(MPU); Wire.write(0x75); Wire.endTransmission(false);
  Wire.requestFrom(MPU, 1);
  return Wire.read() == 0x68;  // WHO_AM_I
}

void setup() {
  // 1. Inicializa I2C, LEDs e Serial
  Serial.begin(115200);
  pinMode(LED_VERDE, OUTPUT);
  pinMode(LED_VERMELHO, OUTPUT);
  Wire.begin(21, 22);
  Wire.setClock(400000);

  // 2. Sensor conectado? Se não, pisca o vermelho e tenta de novo
  while (!sensorConectado()) {
    Serial.println("#ERRO_SENSOR");
    digitalWrite(LED_VERMELHO, !digitalRead(LED_VERMELHO));
    delay(500);
  }
  escreve(0x6B, 0x00);  // acorda o MPU
  escreve(0x1A, 0x03);  // filtro passa-baixa ~42 Hz
  escreve(0x1B, 0x08);  // giroscópio ±500 °/s
  escreve(0x1C, 0x08);  // acelerômetro ±4 g

  // 3. Viés do acelerômetro salvo numa calibração anterior?
  memoria.begin("halter", false);
  if (memoria.getBool("cal", false)) {
    viesA[0] = memoria.getFloat("bx"); viesA[1] = memoria.getFloat("by"); viesA[2] = memoria.getFloat("bz");
    calibrado = true;
  }

  // 4. Calibração: média de 500 amostras em repouso (LED vermelho aceso)
  //    giroscópio → offsets dos 3 eixos · acelerômetro → direção e módulo da gravidade
  digitalWrite(LED_VERMELHO, HIGH);
  float somaA[3] = {0, 0, 0};
  int16_t r[7];
  for (int i = 0; i < 500; i++) {
    while (!le(0x3B, 7, r)) delay(2);
    for (int k = 0; k < 3; k++) { somaA[k] += r[k] / ESC_ACEL * G; offGiro[k] += r[4 + k] / ESC_GIRO; }
    delay(2);
  }
  for (int k = 0; k < 3; k++) { offGiro[k] /= 500; grav[k] = somaA[k] / 500; }
  theta = atan2(grav[1], grav[2]) * RAD_TO_DEG;
  for (int k = 0; k < 3; k++) grav[k] -= viesA[k];
  gLocal = sqrt(grav[0] * grav[0] + grav[1] * grav[1] + grav[2] * grav[2]);
  digitalWrite(LED_VERMELHO, LOW);
  digitalWrite(LED_VERDE, HIGH);
  Serial.println(calibrado ? "#CALIBRADO" : "#CALIBRAR: deixe o haltere parado ~1 s com cada face para cima");

  // 5. BLE
  BLEDevice::init("Haltere");
  BLEDevice::setMTU(100);  // pacote inteiro em uma notificação (o padrão corta em 20 bytes)
  BLEServer *servidor = BLEDevice::createServer();
  servidor->setCallbacks(new Callbacks());
  BLEService *servico = servidor->createService("6E400001-B5A3-F393-E0A9-E50E24DCCA9E");
  tx = servico->createCharacteristic("6E400003-B5A3-F393-E0A9-E50E24DCCA9E", BLECharacteristic::PROPERTY_NOTIFY);
  tx->addDescriptor(new BLE2902());
  servico->start();
  BLEDevice::startAdvertising();
  Serial.println("#PRONTO");

  // 6. Inicia temporizadores
  tAnterior = micros();
}

// Resolve o sistema 4×4 M·x = coluna 4 por eliminação de Gauss-Jordan com pivoteamento parcial
bool resolve4(double M[4][5], double x[4]) {
  for (int c = 0; c < 4; c++) {
    int p = c;
    for (int r = c + 1; r < 4; r++) if (fabs(M[r][c]) > fabs(M[p][c])) p = r;
    if (fabs(M[p][c]) < 1e-9) return false;
    for (int k = 0; k < 5; k++) { double t = M[c][k]; M[c][k] = M[p][k]; M[p][k] = t; }
    for (int r = 0; r < 4; r++) {
      if (r == c) continue;
      double f = M[r][c] / M[c][c];
      for (int k = c; k < 5; k++) M[r][k] -= f * M[c][k];
    }
  }
  for (int i = 0; i < 4; i++) x[i] = M[i][4] / M[i][i];
  return true;
}

// Ajuste de esfera: em repouso |a − b| = g para toda pose. Expandindo, |a|² = 2a·b + c, com c = g² − |b|²,
// que é linear em (b, c) → mínimos quadrados pelas equações normais (AᵀA)x = Aᵀy
void ajustaEsfera() {
  // As poses precisam cobrir direções variadas: det(Σ u·uᵀ / n) ≈ 0,037 com as 6 faces, ≈ 0 num cone estreito
  double S[3][3] = {{0}};
  for (int i = 0; i < nPoses; i++) {
    float n = sqrt(poses[i][0] * poses[i][0] + poses[i][1] * poses[i][1] + poses[i][2] * poses[i][2]);
    for (int r = 0; r < 3; r++) for (int c = 0; c < 3; c++) S[r][c] += poses[i][r] / n * poses[i][c] / n / nPoses;
  }
  double det = S[0][0] * (S[1][1] * S[2][2] - S[1][2] * S[2][1]) - S[0][1] * (S[1][0] * S[2][2] - S[1][2] * S[2][0])
             + S[0][2] * (S[1][0] * S[2][1] - S[1][1] * S[2][0]);
  if (det < 0.004) return;

  double M[4][5] = {{0}};
  for (int i = 0; i < nPoses; i++) {
    double linha[4] = {2 * poses[i][0], 2 * poses[i][1], 2 * poses[i][2], 1};
    double y = poses[i][0] * poses[i][0] + poses[i][1] * poses[i][1] + poses[i][2] * poses[i][2];
    for (int r = 0; r < 4; r++) { for (int c = 0; c < 4; c++) M[r][c] += linha[r] * linha[c]; M[r][4] += linha[r] * y; }
  }
  double x[4];
  if (!resolve4(M, x)) return;
  double b2 = x[0] * x[0] + x[1] * x[1] + x[2] * x[2], g = sqrt(x[3] + b2);
  if (sqrt(b2) > 1.5 || !(g > 9.0 && g < 10.6)) return;   // resultado sem sentido físico: ignora

  for (int k = 0; k < 3; k++) viesA[k] = x[k];
  gLocal = g; viesZ = 0; calibrado = true;
  float n = 0;                                           // reinicia a gravidade pela pose mais recente
  int ult = (proxPose + MAX_POSES - 1) % MAX_POSES;
  for (int k = 0; k < 3; k++) { grav[k] = poses[ult][k] - viesA[k]; n += grav[k] * grav[k]; }
  n = sqrt(n);
  for (int k = 0; k < 3; k++) grav[k] *= gLocal / n;
  memoria.putFloat("bx", viesA[0]); memoria.putFloat("by", viesA[1]); memoria.putFloat("bz", viesA[2]);
  memoria.putBool("cal", true);
  digitalWrite(LED_VERDE, HIGH);
  char msg[80];
  snprintf(msg, sizeof(msg), "#CALIBRADO vies=%.3f,%.3f,%.3f g=%.3f", viesA[0], viesA[1], viesA[2], gLocal);
  avisa(msg);
}

// Fecha uma pose de repouso (média das leituras brutas) e guarda se for uma direção nova
void fechaPose() {
  float m[3], n = 0;
  for (int k = 0; k < 3; k++) { m[k] = somaPose[k] / nPose; n += m[k] * m[k]; somaPose[k] = 0; }
  nPose = 0; n = sqrt(n);
  for (int i = 0; i < nPoses; i++) {
    float ni = sqrt(poses[i][0] * poses[i][0] + poses[i][1] * poses[i][1] + poses[i][2] * poses[i][2]);
    if ((m[0] * poses[i][0] + m[1] * poses[i][1] + m[2] * poses[i][2]) / (n * ni) > POSE_DIST) return;
  }
  for (int k = 0; k < 3; k++) poses[proxPose][k] = m[k];
  proxPose = (proxPose + 1) % MAX_POSES;
  if (nPoses < MAX_POSES) nPoses++;
  if (!calibrado) { char msg[40]; snprintf(msg, sizeof(msg), "#CALIBRANDO %d/%d", min(nPoses, MIN_POSES), MIN_POSES); avisa(msg); }
  if (nPoses >= MIN_POSES) ajustaEsfera();
}

// Altura vertical: integração dupla da aceleração na direção da gravidade, com correções de deriva
void estimaAltura(const float aBruta[3], const float w[3], float dt) {
  float a[3] = {aBruta[0] - viesA[0], aBruta[1] - viesA[1], aBruta[2] - viesA[2]};   // sem o viés de fábrica

  // 1. Gira a gravidade com o giroscópio (vetor fixo no mundo visto do sensor: dg/dt = −ω × g)
  float c0 = w[1] * grav[2] - w[2] * grav[1];
  float c1 = w[2] * grav[0] - w[0] * grav[2];
  float c2 = w[0] * grav[1] - w[1] * grav[0];
  grav[0] -= c0 * dt; grav[1] -= c1 * dt; grav[2] -= c2 * dt;
  // 2. Corrige devagar rumo ao acelerômetro (rápido em repouso, quase nada em movimento)
  float k = parado ? 0.02 : 0.0005;
  float n = 0;
  for (int i = 0; i < 3; i++) { grav[i] += k * (a[i] - grav[i]); n += grav[i] * grav[i]; }
  n = sqrt(n);
  for (int i = 0; i < 3; i++) grav[i] *= gLocal / n;

  // 3. Aceleração vertical = projeção de a na direção "para cima" − g − viés
  float aVert = (a[0] * grav[0] + a[1] * grav[1] + a[2] * grav[2]) / gLocal - gLocal - viesZ;

  // 4. Detecção de repouso (ZUPT): sem aceleração linear e sem rotação por um tempo.
  //    Usa médias móveis com sinal: o tremor oscila em torno de zero e se cancela, o movimento real não
  bufA[iJanela] = sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2]) - gLocal;
  bufM[iJanela] = sqrt(aBruta[0] * aBruta[0] + aBruta[1] * aBruta[1] + aBruta[2] * aBruta[2]);
  for (int i = 0; i < 3; i++) bufW[iJanela][i] = w[i];
  iJanela = (iJanela + 1) % JANELA;
  float mA = 0, mW[3] = {0, 0, 0}, mMin = 1e9, mMax = 0;
  for (int j = 0; j < JANELA; j++) {
    mA += bufA[j]; for (int i = 0; i < 3; i++) mW[i] += bufW[j][i];
    mMin = min(mMin, bufM[j]); mMax = max(mMax, bufM[j]);
  }
  mA /= JANELA;
  float modW = sqrt(mW[0] * mW[0] + mW[1] * mW[1] + mW[2] * mW[2]) / JANELA * RAD_TO_DEG;
  bool quieto = fabs(mA) < REPOUSO_ACEL && modW < REPOUSO_GIRO;

  // Repouso firme: sem rotação e |a| constante. Não depende do valor de g, então continua funcionando quando
  // o chip esquenta e o g medido ao ligar fica desatualizado
  bool firme = modW < FIRME_GIRO && mMax - mMin < FIRME_ACEL;
  nFirme = firme ? nFirme + 1 : 0;
  if (nFirme >= FIRME_MIN && parado) {
    // O aquecimento muda devagar o g medido e o desvio do giroscópio: num repouso firme, acompanha os dois
    gLocal += APRENDE * (bufA[(iJanela + JANELA - 1) % JANELA]);   // |a sem viés| − g da leitura atual → 0
    for (int i = 0; i < 3; i++) offGiro[i] += APRENDE * w[i] * RAD_TO_DEG;   // parado, ω deveria ser 0
  }

  // Repouso firme: junta leituras brutas para a calibração do viés
  if (firme) {
    for (int k = 0; k < 3; k++) somaPose[k] += aBruta[k];
    if (++nPose == 100) fechaPose();                    // 1 s parado → uma pose
  } else {
    if (nPose >= 30) fechaPose();                        // parou pelo menos 0,3 s
    for (int k = 0; k < 3; k++) somaPose[k] = 0;
    nPose = 0;
  }
  nQuieto = quieto ? nQuieto + 1 : 0;
  // a velocidade alta evita confundir o meio de um movimento lento (aceleração ≈ 0) com repouso
  bool agoraParado = (nQuieto >= REPOUSO_CURTO && fabs(vz) < REPOUSO_VEL) || nQuieto >= REPOUSO_LONGO;

  if (agoraParado) {
    // Ao parar, a velocidade deveria ser 0. Supondo que o erro cresceu linearmente durante o movimento,
    // o erro acumulado na altura é vz·tMov/2 → desconta de uma vez
    // (só vale para movimentos curtos: depois de MOV_MAX a velocidade já foi amortecida e a hipótese não vale)
    if (!parado && tMov <= MOV_MAX) z -= vz * tMov / 2;
    vz = 0; tMov = 0;
    viesZ += 0.01 * aVert;               // em repouso aVert deveria ser 0: o que sobra é viés
  } else {
    // 5. Integração (Euler): v ← v + a·Δt ; z ← z + v·Δt
    vz += aVert * dt;
    if (tMov > MOV_MAX) vz -= vz * dt / TAU_FUGA;   // repouso não detectado há muito tempo: evita a deriva sem limite
    z += vz * dt;
    tMov += dt;
  }
  parado = agoraParado;
}

void loop() {
  // Comando pelo Monitor Serial: R apaga a calibração do viés e reinicia
  if (Serial.available() && toupper(Serial.read()) == 'R') { memoria.clear(); ESP.restart(); }
  if (!calibrado) digitalWrite(LED_VERDE, (millis() / 250) % 2);   // pisca enquanto não calibrou
  static unsigned long ultimoAviso = 0;                              // lembra o jogo a cada 2 s
  if (!calibrado && millis() - ultimoAviso > 2000) {
    ultimoAviso = millis();
    char msg[40]; snprintf(msg, sizeof(msg), "#CALIBRANDO %d/%d", min(nPoses, MIN_POSES), MIN_POSES); avisa(msg);
  }

  unsigned long agora = micros();
  if (agora - tAnterior < 10000) return;  // 100 Hz
  float dt = (agora - tAnterior) / 1e6;
  tAnterior = agora;

  // Lê acelerômetro, temperatura (ignorada) e giroscópio de uma vez
  int16_t r[7];
  if (!le(0x3B, 7, r)) return;                          // leitura I2C falhou: descarta
  if (r[0] == -1 && r[1] == -1 && r[2] == -1 && r[4] == -1) return;   // barramento devolveu só 0xFF: descarta

  // Corrige offset e converte unidades
  float ax = r[0] / ESC_ACEL * G, ay = r[1] / ESC_ACEL * G, az = r[2] / ESC_ACEL * G;  // m/s²
  float wx = r[4] / ESC_GIRO - offGiro[0];                                           // °/s
  float wy = r[5] / ESC_GIRO - offGiro[1];
  float wz = r[6] / ESC_GIRO - offGiro[2];

  // Altura vertical (usa a aceleração bruta, com gravidade, e os 3 eixos do giroscópio; só o movimento vertical conta)
  float a[3] = {ax, ay, az}, w[3] = {(float)(wx * DEG_TO_RAD), (float)(wy * DEG_TO_RAD), (float)(wz * DEG_TO_RAD)};
  estimaAltura(a, w, dt);

  // Estima orientação: Euler (giroscópio) corrigido por descida do gradiente rumo ao acelerômetro
  float thetaAcel = atan2(ay, az) * RAD_TO_DEG;
  float thetaEuler = theta + wx * dt;
  theta = thetaEuler - BETA * (thetaEuler - thetaAcel);

  // Remove a gravidade dos eixos Y e Z
  float th = theta * DEG_TO_RAD;
  ay -= G * sin(th);
  az -= G * cos(th);

  // A cada 2 leituras (20 ms → 50 Hz) monta e transmite o pacote
  if (++contador < 2) return;
  contador = 0;
  char pacote[96];
  snprintf(pacote, sizeof(pacote), "%lu,%.2f,%.3f,%.3f,%.3f,%.1f,%d",
           millis(), theta, ax, ay, az, z * 100, parado ? 1 : 0);
  Serial.println(pacote);
  if (conectado) { tx->setValue((uint8_t *)pacote, strlen(pacote)); tx->notify(); }
}
