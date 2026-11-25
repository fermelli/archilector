// Firmware de prueba T2.3 (Documento Técnico, tabla 4.6):
// activa cada salida de la tabla 4.3 por turno y gira el motor en ambos sentidos.
// También cubre T2.4 (escaneo I2C) y T2.5 (lectura de las 16 teclas).
//
//   pio run -e prueba_salidas -t upload && pio device monitor
//
// ¡Con el mecanismo montado, el brazo y la barra se moverán!
#ifdef PRUEBA_SALIDAS

#include <Arduino.h>
#include <Wire.h>
#include "config.h"
#include "lcd_i2c.h"
#include "pwm.h"

static LcdI2C lcd;

static const char MAPA[4][4] = {
    {'1', '2', '3', 'A'}, {'4', '5', '6', 'B'}, {'7', '8', '9', 'C'}, {'*', '0', '#', 'D'}};
static const uint8_t SECUENCIA[8][4] = {{1, 0, 0, 0}, {1, 1, 0, 0}, {0, 1, 0, 0}, {0, 1, 1, 0},
                                        {0, 0, 1, 0}, {0, 0, 1, 1}, {0, 0, 0, 1}, {1, 0, 0, 1}};

static void servo(uint8_t pin, uint8_t canal, int angulo) {
  uint32_t us = 500 + (uint32_t)(2000L * angulo / 180);
  pwmEscribir(pin, canal, servoCicloDesdeMicros(us));
}

static void motor(int pasos, int dir) {
  int fase = 0;
  for (int i = 0; i < pasos; i++) {
    fase = (fase + dir + 8) % 8;
    for (int b = 0; b < 4; b++) digitalWrite(PIN_MOTOR[b], SECUENCIA[fase][b]);
    delayMicroseconds(1666);
  }
  for (int b = 0; b < 4; b++) digitalWrite(PIN_MOTOR[b], LOW);
}

static void escanearI2C() {
  Serial.println("[T2.4] Escaneo I2C:");
  for (uint8_t d = 1; d < 127; d++) {
    Wire.beginTransmission(d);
    if (Wire.endTransmission() == 0) Serial.printf("  dispositivo en 0x%02X\n", d);
  }
}

static char leerTecla() {
  char t = 0;
  for (int f = 0; f < 4; f++) {
    digitalWrite(PIN_FILAS[f], LOW);
    delayMicroseconds(5);
    for (int c = 0; c < 4; c++)
      if (digitalRead(PIN_COLUMNAS[c]) == LOW) t = MAPA[f][c];
    digitalWrite(PIN_FILAS[f], HIGH);
  }
  return t;
}

void setup() {
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);
  for (int i = 0; i < 4; i++) {
    pinMode(PIN_MOTOR[i], OUTPUT);
    digitalWrite(PIN_MOTOR[i], LOW);
    pinMode(PIN_FILAS[i], OUTPUT);
    digitalWrite(PIN_FILAS[i], HIGH);
    pinMode(PIN_COLUMNAS[i], INPUT);
  }
  Serial.begin(SERIAL_BAUDIOS);
  delay(500);
  Serial.println("\n=== ArchiLector · prueba de salidas T2.3 ===");

  bool hayLcd = lcd.iniciar();
  escanearI2C();
  Serial.printf("LCD: %s (0x%02X)\n", hayLcd ? "encontrada" : "NO encontrada", lcd.direccion());
  lcd.escribirLinea(0, "PRUEBA T2.3");

  pwmIniciar(PIN_SERVO_BRAZO, CANAL_BRAZO, SERVO_FREQ_HZ, SERVO_RES_BITS);
  pwmIniciar(PIN_SERVO_BARRA, CANAL_BARRA, SERVO_FREQ_HZ, SERVO_RES_BITS);
  pwmIniciar(PIN_LED, CANAL_LED, LED_FREQ_HZ, LED_RES_BITS);
  servo(PIN_SERVO_BRAZO, CANAL_BRAZO, 90);
  servo(PIN_SERVO_BARRA, CANAL_BARRA, 0);
}

void loop() {
  Serial.println("\n[1] Fases del motor una por una (GPIO 32, 33, 25, 26)");
  for (int i = 0; i < 4; i++) {
    Serial.printf("  GPIO%d en alto\n", PIN_MOTOR[i]);
    lcd.escribirLinea(1, "MOTOR FASE");
    digitalWrite(PIN_MOTOR[i], HIGH);
    delay(700);
    digitalWrite(PIN_MOTOR[i], LOW);
  }

  Serial.println("[2] Motor: media vuelta en cada sentido");
  lcd.escribirLinea(1, "MOTOR GIRO");
  motor(2048, 1);
  delay(300);
  motor(2048, -1);

  Serial.println("[3] Servo brazos (GPIO18): 90 -> 60 -> 120 -> 90");
  lcd.escribirLinea(1, "SERVO BRAZO");
  for (int a : {60, 120, 90}) {
    servo(PIN_SERVO_BRAZO, CANAL_BRAZO, a);
    delay(800);
  }

  Serial.println("[4] Servo barra (GPIO19): 0 -> 45 -> 0");
  lcd.escribirLinea(1, "SERVO BARRA");
  servo(PIN_SERVO_BARRA, CANAL_BARRA, 45);
  delay(700);
  servo(PIN_SERVO_BARRA, CANAL_BARRA, 0);
  delay(700);

  Serial.println("[5] Tira LED (GPIO27): rampa 0 -> 100 % -> 0");
  lcd.escribirLinea(1, "LUZ LED");
  for (int p = 0; p <= 100; p += 5) {
    pwmEscribir(PIN_LED, CANAL_LED, ((1 << LED_RES_BITS) - 1) * p / 100);
    delay(40);
  }
  pwmEscribir(PIN_LED, CANAL_LED, 0);

  Serial.println("[6] Buzzer (GPIO4): un pitido");
  lcd.escribirLinea(1, "BUZZER");
  digitalWrite(PIN_BUZZER, HIGH);
  delay(200);
  digitalWrite(PIN_BUZZER, LOW);

  Serial.println("[7] Teclado (T2.5): pulse teclas durante 8 s");
  lcd.escribirLinea(1, "PULSE TECLAS");
  uint32_t t0 = millis();
  char anterior = 0;
  while (millis() - t0 < 8000) {
    char t = leerTecla();
    if (t && t != anterior) {
      Serial.printf("  tecla %c\n", t);
      char linea[17];
      snprintf(linea, sizeof(linea), "TECLA %c", t);
      lcd.escribirLinea(1, linea);
    }
    anterior = t;
    delay(30);
  }
  Serial.println("--- ciclo completo; se repite ---");
}

#endif  // PRUEBA_SALIDAS
