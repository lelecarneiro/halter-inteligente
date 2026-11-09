/*
 * Halter Inteligente — firmware ESP32 + MPU-6050
 * Lê o IMU a 100 Hz, estima o ângulo θ e a altura vertical z do haltere e transmite a 50 Hz por BLE
 * (e Serial, para depuração).
 * Pacote CSV: t_ms,theta_graus,ax,ay,az,z_cm,parado
 *   ax, ay, az = aceleração linear em m/s² · z_cm = altura relativa (cm, positivo para cima)
 *   parado = 1 quando o haltere está em repouso (o jogo usa para corrigir a deriva da altura)
 */
#include <Wire.h>
#include <BLEDevice.h>
#include <BLE2902.h>

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
const float REPOUSO_GIRO = 5.0;      // °/s   — média da rotação abaixo disso
const float REPOUSO_VEL  = 0.10;     // m/s   — velocidade estimada abaixo disso
const int   REPOUSO_CURTO = 10;      // 0,1 s quieto e com velocidade baixa → parado
const int   REPOUSO_LONGO = 150;     // 1,5 s quieto → parado, qualquer que seja a velocidade estimada
const float MOV_MAX = 3.0;           // s sem repouso: a partir daí a velocidade é amortecida (limita a deriva)
const float TAU_FUGA = 2.0;          // s — constante de tempo desse amortecimento

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
int nQuieto = 0, iJanela = 0;
float bufA[JANELA], bufW[JANELA][3];   // janelas da média móvel (zeradas: variáveis globais)
bool parado = true;
unsigned long tAnterior;
int contador = 0;

// ---------- Funções auxiliares de I2C ----------
void escreve(byte reg, byte val) {
  Wire.beginTransmission(MPU); Wire.write(reg); Wire.write(val); Wire.endTransmission();
}
void le(byte reg, int n, int16_t *dest) {  // lê n palavras de 16 bits a partir de reg
  Wire.beginTransmission(MPU); Wire.write(reg); Wire.endTransmission(false);
  Wire.requestFrom(MPU, n * 2);
  for (int i = 0; i < n; i++) { int alto = Wire.read(); dest[i] = (alto << 8) | Wire.read(); }
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

  // 3. Calibração: média de 500 amostras em repouso (LED vermelho aceso)
  //    giroscópio → offsets dos 3 eixos · acelerômetro → direção e módulo da gravidade
  digitalWrite(LED_VERMELHO, HIGH);
  float somaA[3] = {0, 0, 0};
  int16_t r[7];
  for (int i = 0; i < 500; i++) {
    le(0x3B, 7, r);
    for (int k = 0; k < 3; k++) { somaA[k] += r[k] / ESC_ACEL * G; offGiro[k] += r[4 + k] / ESC_GIRO; }
    delay(2);
  }
  for (int k = 0; k < 3; k++) { grav[k] = somaA[k] / 500; offGiro[k] /= 500; }
  gLocal = sqrt(grav[0] * grav[0] + grav[1] * grav[1] + grav[2] * grav[2]);
  theta = atan2(grav[1], grav[2]) * RAD_TO_DEG;
  digitalWrite(LED_VERMELHO, LOW);
  digitalWrite(LED_VERDE, HIGH);

  // 4. BLE
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

  // 5. Inicia temporizadores
  tAnterior = micros();
}

// Altura vertical: integração dupla da aceleração na direção da gravidade, com correções de deriva
void estimaAltura(const float a[3], const float w[3], float dt) {
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
  for (int i = 0; i < 3; i++) bufW[iJanela][i] = w[i];
  iJanela = (iJanela + 1) % JANELA;
  float mA = 0, mW[3] = {0, 0, 0};
  for (int j = 0; j < JANELA; j++) { mA += bufA[j]; for (int i = 0; i < 3; i++) mW[i] += bufW[j][i]; }
  mA /= JANELA;
  float modW = sqrt(mW[0] * mW[0] + mW[1] * mW[1] + mW[2] * mW[2]) / JANELA * RAD_TO_DEG;
  bool quieto = fabs(mA) < REPOUSO_ACEL && modW < REPOUSO_GIRO;
  nQuieto = quieto ? nQuieto + 1 : 0;
  // a velocidade alta evita confundir o meio de um movimento lento (aceleração ≈ 0) com repouso
  bool agoraParado = (nQuieto >= REPOUSO_CURTO && fabs(vz) < REPOUSO_VEL) || nQuieto >= REPOUSO_LONGO;

  if (agoraParado) {
    // Ao parar, a velocidade deveria ser 0. Supondo que o erro cresceu linearmente durante o movimento,
    // o erro acumulado na altura é vz·tMov/2 → desconta de uma vez
    if (!parado) z -= vz * tMov / 2;
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
  unsigned long agora = micros();
  if (agora - tAnterior < 10000) return;  // 100 Hz
  float dt = (agora - tAnterior) / 1e6;
  tAnterior = agora;

  // Lê acelerômetro, temperatura (ignorada) e giroscópio de uma vez
  int16_t r[7];
  le(0x3B, 7, r);

  // Corrige offset e converte unidades
  float ax = r[0] / ESC_ACEL * G, ay = r[1] / ESC_ACEL * G, az = r[2] / ESC_ACEL * G;  // m/s²
  float wx = r[4] / ESC_GIRO - offGiro[0];                                           // °/s
  float wy = r[5] / ESC_GIRO - offGiro[1];
  float wz = r[6] / ESC_GIRO - offGiro[2];

  // Altura vertical (usa a aceleração bruta, com gravidade, e os 3 eixos do giroscópio)
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
