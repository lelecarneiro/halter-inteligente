/*
 * Halter Inteligente — firmware ESP32 + MPU-6050
 * Lê o IMU a 100 Hz, filtra (passa-baixa 12 Hz), estima o ângulo θ e transmite a 50 Hz por BLE (e Serial).
 * Seções do memorial de cálculo: 5.1 (discretização), 5.2 (calibração), 5.4 (ângulo), 5.4.3 (gravidade), 5.7 (filtro).
 * Dois ângulos são estimados pelo mesmo método (extrapolação + gradiente descendente):
 *   θ (rotação em X, o movimento do exercício) e φ (inclinação lateral, rotação em Y), para que a gravidade
 *   seja retirada dos três eixos — sem φ, parte de g "vaza" para ax e vira um falso desvio lateral.
 * Pacote CSV: t_ms,theta_graus,ax,ay,az   (aceleração linear em m/s²)
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
const float ALFA = 0.1;         // 5.4.2 passo α do gradiente descendente
const float DT = 0.01;          // 5.1 Δt_sensor = 1/100 Hz
const unsigned long PERIODO_US = 10000;  // 10 ms entre leituras (100 Hz)
// 5.7 Filtro digital passa-baixa de 1ª ordem com corte em 12 Hz: mantém os tremores (3–12 Hz) e remove ruído
const float FC_FILTRO = 12.0;
const float K_FILTRO = DT / (1.0 / (2 * PI * FC_FILTRO) + DT);   // y_k = y_k-1 + K·(x_k − y_k-1), K ≈ 0,43

// ---------- BLE (serviço UART da Nordic) ----------
BLECharacteristic *tx;
bool conectado = false;
struct Callbacks : BLEServerCallbacks {
  void onConnect(BLEServer *s) { conectado = true; }
  void onDisconnect(BLEServer *s) { conectado = false; BLEDevice::startAdvertising(); }
};

// ---------- Estado ----------
float offsetGx = 0, offsetGy = 0, offsetGz = 0;  // E_medio de cada eixo do giroscópio (°/s)
float theta = 0, phi = 0;                        // θ: ângulo do exercício · φ: inclinação lateral
float wAnterior = 0, wPhiAnterior = 0;           // ω(t_k-1) de θ e de φ, usadas na extrapolação
float fAx = 0, fAy = 0, fAz = G, fWx = 0, fWy = 0, fWz = 0;  // saídas do filtro de 12 Hz
bool primeiraLeitura = true;
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

  // 3. Calibração (5.2): E_medio = (1/500)·Σ leitura_parado,i — 500 amostras em repouso (LED vermelho aceso)
  digitalWrite(LED_VERMELHO, HIGH);
  int16_t g[3];
  for (int i = 0; i < 500; i++) {
    le(0x43, 3, g);
    offsetGx += g[0] / ESC_GIRO; offsetGy += g[1] / ESC_GIRO; offsetGz += g[2] / ESC_GIRO;
    delay(2);
  }
  offsetGx /= 500; offsetGy /= 500; offsetGz /= 500;
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

void loop() {
  // 5.1 Amostragem em cadência fixa de 100 Hz: cada passo vale exatamente Δt = 0,01 s
  unsigned long agora = micros();
  if (agora - tAnterior < PERIODO_US) return;
  tAnterior += PERIODO_US;
  if (agora - tAnterior > 5 * PERIODO_US) tAnterior = agora;  // atraso grande (ex.: BLE): ressincroniza

  // Lê acelerômetro, temperatura (ignorada) e giroscópio de uma vez
  int16_t r[7];
  le(0x3B, 7, r);

  // 5.2 Converte unidades e corrige o offset: a = (a_lida/8192)·9,81 [m/s²]; ω = (ω_lido/65,5) − E_medio [°/s]
  float ax = r[0] / ESC_ACEL * G, ay = r[1] / ESC_ACEL * G, az = r[2] / ESC_ACEL * G;
  float wx = r[4] / ESC_GIRO - offsetGx, wy = r[5] / ESC_GIRO - offsetGy, wz = r[6] / ESC_GIRO - offsetGz;

  // 5.7 Filtro digital passa-baixa de 12 Hz aplicado a cada leitura recebida
  if (primeiraLeitura) { fAx = ax; fAy = ay; fAz = az; fWx = wx; fWy = wy; fWz = wz; }
  fAx += K_FILTRO * (ax - fAx); fAy += K_FILTRO * (ay - fAy); fAz += K_FILTRO * (az - fAz);
  fWx += K_FILTRO * (wx - fWx); fWy += K_FILTRO * (wy - fWy); fWz += K_FILTRO * (wz - fWz);
  ax = fAx; ay = fAy; az = fAz; wx = fWx; wy = fWy; wz = fWz;

  // 5.4.1 Ângulo de referência da gravidade: θ_acel = arctan(ay/az) (atan2 = arco-tangente válida nos 4 quadrantes)
  float thetaAcel = atan2(ay, az) * RAD_TO_DEG;
  // Inclinação lateral pela gravidade: φ_acel = arctan(−ax / √(ay² + az²))
  float phiAcel = atan2(-ax, sqrt(ay * ay + az * az)) * RAD_TO_DEG;
  // Velocidade de φ em ângulos de Euler (rotação em X seguida de Y): φ' = ωy·cos θ − ωz·sen θ
  float wPhi = wy * cos(theta * DEG_TO_RAD) - wz * sin(theta * DEG_TO_RAD);
  if (primeiraLeitura) {
    theta = thetaAcel; wAnterior = wx; phi = phiAcel; wPhiAnterior = wPhi; primeiraLeitura = false;
  }
  // 5.4 Extrapolação linear (Taylor de 1ª ordem): θ_prev(t_k) = θ(t_k-1) + ω(t_k-1)·Δt
  float thetaPrev = theta + wAnterior * DT;
  // 5.4.2 Gradiente descendente: θ(t_k) = θ_prev(t_k) − α·(θ_prev − θ_acel)
  // A diferença é levada a (−180°, 180°]: atan2 salta de +180° para −180°, e θ continua contínuo (sem "dar a volta")
  float erroTheta = thetaPrev - thetaAcel;
  erroTheta -= 360.0 * round(erroTheta / 360.0);
  theta = thetaPrev - ALFA * erroTheta;
  wAnterior = wx;
  // Mesmo método para φ: extrapolação linear + gradiente descendente
  float phiPrev = phi + wPhiAnterior * DT;
  phi = phiPrev - ALFA * (phiPrev - phiAcel);
  wPhiAnterior = wPhi;

  // 5.4.3 Subtração da gravidade nos três eixos, g_sensor = 9,81·(−sen φ, cos φ·sen θ, cos φ·cos θ):
  //   a_lin,x = ax + 9,81·sen φ;  a_lin,y = ay − 9,81·cos φ·sen θ;  a_lin,z = az − 9,81·cos φ·cos θ
  float th = theta * DEG_TO_RAD, ph = phi * DEG_TO_RAD;
  ax += G * sin(ph);
  ay -= G * cos(ph) * sin(th);
  az -= G * cos(ph) * cos(th);

  // 5.1 A cada 2 leituras (Δt_envio = 0,02 s → 50 Hz) monta e transmite o pacote
  if (++contador < 2) return;
  contador = 0;
  char pacote[64];
  snprintf(pacote, sizeof(pacote), "%lu,%.2f,%.3f,%.3f,%.3f", millis(), theta, ax, ay, az);
  Serial.println(pacote);
  if (conectado) { tx->setValue((uint8_t *)pacote, strlen(pacote)); tx->notify(); }
}
