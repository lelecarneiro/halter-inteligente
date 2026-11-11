/*
 * Halter Inteligente — firmware ESP32 + MPU-6050
 * Segue o memorial de cálculo, seções 5.1 a 5.4.3:
 *   5.1  lê o IMU a 100 Hz (Δt = 0,01 s) e transmite a 50 Hz (Δt = 0,02 s) por BLE (e Serial, para depuração)
 *   5.2  calibração do giroscópio (média de 500 amostras) e conversão para unidades físicas
 *   5.4  ângulo θ: extrapolação linear (Taylor de 1ª ordem) corrigida por gradiente descendente
 *   5.4.3 subtração da gravidade → aceleração linear
 *   5.7  filtro digital do sensor que mantém tremores até 12 Hz
 * Pacote CSV: t_ms,theta_graus,ax,ay,az   (aceleração linear em m/s²)
 * Velocidade e posição (5.5, trapézios) são calculadas no computador, para poupar a ESP32.
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
const float BETA = 0.1;         // α: passo do gradiente descendente (5.4.2)

// ---------- BLE (serviço UART da Nordic) ----------
BLECharacteristic *tx;
bool conectado = false;
struct Callbacks : BLEServerCallbacks {
  void onConnect(BLEServer *s) { conectado = true; }
  void onDisconnect(BLEServer *s) { conectado = false; BLEDevice::startAdvertising(); }
};

// ---------- Estado ----------
float offsetGx = 0, theta = 0, wAnterior = 0;
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
  escreve(0x1A, 0x04);  // 5.7 filtro passa-baixa digital ~21 Hz: mantém os tremores (3–12 Hz) e tira ruído
  escreve(0x1B, 0x08);  // giroscópio ±500 °/s
  escreve(0x1C, 0x08);  // acelerômetro ±4 g

  // 3. 5.2 Calibração: E_médio = (1/500)·Σ leitura parado, com o haltere em repouso (LED vermelho aceso)
  digitalWrite(LED_VERMELHO, HIGH);
  int16_t gx;
  for (int i = 0; i < 500; i++) { le(0x43, 1, &gx); offsetGx += gx / ESC_GIRO; delay(2); }
  offsetGx /= 500;
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
  unsigned long agora = micros();
  if (agora - tAnterior < 10000) return;  // 100 Hz
  float dt = (agora - tAnterior) / 1e6;
  tAnterior = agora;

  // Lê acelerômetro, temperatura (ignorada) e giroscópio de uma vez
  int16_t r[7];
  le(0x3B, 7, r);

  // 5.2 Conversão de unidades: a = (a_lida / 8192)·9,81 [m/s²] ; ω = (ω_lido / 65,5) − E_médio [°/s]
  float ax = r[0] / ESC_ACEL * G, ay = r[1] / ESC_ACEL * G, az = r[2] / ESC_ACEL * G;
  float wx = r[4] / ESC_GIRO - offsetGx;

  // 5.4 Extrapolação linear: θ_prev(t_k) = θ(t_k−1) + ω(t_k−1)·Δt
  float thetaPrev = theta + wAnterior * dt;
  wAnterior = wx;
  // 5.4.1 Ângulo de referência pela gravidade: θ_aceleração = arctan(a_y / a_z)
  float thetaAcel = atan2(ay, az) * RAD_TO_DEG;
  // 5.4.2 Gradiente descendente sobre E(θ) = ½(θ − θ_aceleração)²: θ(t_k) = θ_prev − α·(θ_prev − θ_aceleração)
  theta = thetaPrev - BETA * (thetaPrev - thetaAcel);

  // 5.4.3 Subtração da gravidade: a_lin,y = a_y − 9,81·sen θ ; a_lin,z = a_z − 9,81·cos θ
  float th = theta * DEG_TO_RAD;
  ay -= G * sin(th);
  az -= G * cos(th);

  // A cada 2 leituras (20 ms → 50 Hz) monta e transmite o pacote
  if (++contador < 2) return;
  contador = 0;
  char pacote[64];
  snprintf(pacote, sizeof(pacote), "%lu,%.2f,%.3f,%.3f,%.3f", millis(), theta, ax, ay, az);
  Serial.println(pacote);
  if (conectado) { tx->setValue((uint8_t *)pacote, strlen(pacote)); tx->notify(); }
}
