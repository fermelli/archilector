#include "interfaz.h"
#include "config.h"
#include "estado.h"
#include "lcd_i2c.h"
#include "protocolo.h"
#include "pwm.h"

static LcdI2C lcd;

// ---------------------------------------------------------------------------
// LCD: las demás tareas solo dejan el texto; esta tarea lo dibuja
// ---------------------------------------------------------------------------
static portMUX_TYPE muxLcd = portMUX_INITIALIZER_UNLOCKED;
static char textoLcd[2][17] = {"", ""};
static volatile bool lcdPendiente = false;

static void copiar16(char* destino, const char* origen) {
  strncpy(destino, origen, 16);
  destino[16] = '\0';
}

void lcdMostrar(const char* linea1, const char* linea2) {
  portENTER_CRITICAL(&muxLcd);
  copiar16(textoLcd[0], linea1);
  copiar16(textoLcd[1], linea2);
  lcdPendiente = true;
  portEXIT_CRITICAL(&muxLcd);
}

void lcdLinea(uint8_t linea, const char* texto) {
  if (linea < 1 || linea > 2) return;
  portENTER_CRITICAL(&muxLcd);
  copiar16(textoLcd[linea - 1], texto);
  lcdPendiente = true;
  portEXIT_CRITICAL(&muxLcd);
}

bool lcdPresente() { return lcd.presente(); }

// ---------------------------------------------------------------------------
// Buzzer: patrones no bloqueantes (tabla 7.3)
// ---------------------------------------------------------------------------
struct Tramo {
  uint16_t encendidoMs;
  uint16_t apagadoMs;
};
static const Tramo PATRON_CORTO[] = {{120, 0}};
static const Tramo PATRON_LARGO[] = {{600, 0}};
static const Tramo PATRON_TRES_LARGOS[] = {{500, 250}, {500, 250}, {500, 0}};

static volatile uint8_t patronPedido = 0;
static const Tramo* patron = nullptr;
static uint8_t tramos = 0, tramoActual = 0;
static bool sonando = false;
static uint32_t marcaBuzzer = 0;

void buzzerPatron(uint8_t p) { patronPedido = p; }

static void atenderBuzzer() {
  uint8_t pedido = patronPedido;
  if (pedido) {
    patronPedido = 0;
    switch (pedido) {
      case 1: patron = PATRON_CORTO; tramos = 1; break;
      case 2: patron = PATRON_LARGO; tramos = 1; break;
      default: patron = PATRON_TRES_LARGOS; tramos = 3; break;
    }
    tramoActual = 0;
    sonando = true;
    marcaBuzzer = millis();
    digitalWrite(PIN_BUZZER, HIGH);
    return;
  }
  if (!patron) return;
  uint32_t t = millis() - marcaBuzzer;
  const Tramo& tr = patron[tramoActual];
  if (sonando && t >= tr.encendidoMs) {
    digitalWrite(PIN_BUZZER, LOW);
    sonando = false;
    marcaBuzzer = millis();
    if (tr.apagadoMs == 0) patron = nullptr;
  } else if (!sonando && t >= tr.apagadoMs) {
    tramoActual++;
    if (tramoActual >= tramos) {
      patron = nullptr;
    } else {
      sonando = true;
      marcaBuzzer = millis();
      digitalWrite(PIN_BUZZER, HIGH);
    }
  }
}

// ---------------------------------------------------------------------------
// Tira LED
// ---------------------------------------------------------------------------
void luzPorcentaje(uint8_t porcentaje) {
  if (porcentaje > 100) porcentaje = 100;
  const uint32_t maximo = (1UL << LED_RES_BITS) - 1;
  pwmEscribir(PIN_LED, CANAL_LED, maximo * porcentaje / 100);
}

// ---------------------------------------------------------------------------
// Teclado 4x4: filas como salidas (una en bajo a la vez), columnas con pull-up
// externo de 10 kΩ porque GPIO 34-39 no tienen pull-up interno.
// ---------------------------------------------------------------------------
static const char MAPA[4][4] = {
    {'1', '2', '3', 'A'},
    {'4', '5', '6', 'B'},
    {'7', '8', '9', 'C'},
    {'*', '0', '#', 'D'},
};

static char leerTecla() {
  char hallada = 0;
  for (int f = 0; f < 4; f++) {
    digitalWrite(PIN_FILAS[f], LOW);
    delayMicroseconds(5);
    for (int c = 0; c < 4; c++) {
      if (digitalRead(PIN_COLUMNAS[c]) == LOW) hallada = MAPA[f][c];
    }
    digitalWrite(PIN_FILAS[f], HIGH);
  }
  return hallada;
}

static void teclaPulsada(char tecla) {
  if (tecla == 'D') {
    // Parada local (tabla 7.2): primero corta los actuadores, después avisa
    entrarParada("PARADA", true);
    enviar(0, "EVT,KEY,D");
    return;
  }
  char cuerpo[16];
  snprintf(cuerpo, sizeof(cuerpo), "EVT,KEY,%c", tecla);
  enviar(0, cuerpo);
}

void interfazIniciar() {
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);
  for (int f = 0; f < 4; f++) {
    pinMode(PIN_FILAS[f], OUTPUT);
    digitalWrite(PIN_FILAS[f], HIGH);
  }
  for (int c = 0; c < 4; c++) pinMode(PIN_COLUMNAS[c], INPUT);
  pwmIniciar(PIN_LED, CANAL_LED, LED_FREQ_HZ, LED_RES_BITS);
  luzPorcentaje(0);
  lcd.iniciar();
  lcd.escribirLinea(0, "ARCHILECTOR " FW_VERSION);
  lcd.escribirLinea(1, "ARRANQUE");
}

void interfazTarea(void*) {
  const uint32_t PERIODO_MS = 10;
  const uint8_t LECTURAS_ESTABLES = 3;  // 30 ms sin cambios = tecla válida (T2.5)
  char candidata = 0, estable = 0;
  uint8_t cuenta = 0;
  uint32_t ultimaLcd = 0;

  for (;;) {
    char t = leerTecla();
    if (t == candidata) {
      if (cuenta < LECTURAS_ESTABLES) cuenta++;
      if (cuenta == LECTURAS_ESTABLES && t != estable) {
        estable = t;
        if (t) teclaPulsada(t);
      }
    } else {
      candidata = t;
      cuenta = 0;
    }

    atenderBuzzer();

    // La LCD por I2C es lenta: se redibuja como máximo cada 50 ms
    if (lcdPendiente && millis() - ultimaLcd >= 50) {
      char l1[17], l2[17];
      portENTER_CRITICAL(&muxLcd);
      memcpy(l1, textoLcd[0], 17);
      memcpy(l2, textoLcd[1], 17);
      lcdPendiente = false;
      portEXIT_CRITICAL(&muxLcd);
      lcd.escribirLinea(0, l1);
      lcd.escribirLinea(1, l2);
      ultimaLcd = millis();
    }
    vTaskDelay(pdMS_TO_TICKS(PERIODO_MS));
  }
}
