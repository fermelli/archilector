// ArchiLector · configuración fija del firmware (Documento Técnico v2.2)
#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// Versiones (sección 10.3): PING responde PONG,<firmware>,<protocolo>
// ---------------------------------------------------------------------------
#define FW_VERSION    "2.3.0"
#define PROTO_VERSION "P1"

// ---------------------------------------------------------------------------
// Pines · tabla 4.3 del Documento Técnico y tabla 2b del Documento de Diseño
// (iguales desde la versión 2.3).
// ---------------------------------------------------------------------------
static const uint8_t PIN_MOTOR[4] = {32, 33, 25, 26};  // ULN2003 IN1..IN4 (cable C09)
#define PIN_SERVO_BRAZO 18                              // MG996R (C07), LEDC 50 Hz 16 bit
#define PIN_SERVO_BARRA 19                              // SG90 (C08),   LEDC 50 Hz 16 bit
#define PIN_LED         27                              // compuerta IRLZ44N (C14), LEDC 1 kHz
#define PIN_BUZZER       4                              // base 2N2222 (C13)
#define PIN_SDA         21                              // I2C de la LCD opcional; libre desde v2.3
#define PIN_SCL         22                              // I2C de la LCD opcional; libre desde v2.3
static const uint8_t PIN_FILAS[4]    = {13, 16, 17, 23};  // salidas, una en bajo a la vez
static const uint8_t PIN_COLUMNAS[4] = {34, 35, 36, 39};  // solo entrada, pull-up externo 10 kΩ

// Canales LEDC (core 2.x: los canales 2n y 2n+1 comparten temporizador)
#define CANAL_BRAZO 0
#define CANAL_BARRA 1
#define CANAL_LED   2

#define SERVO_FREQ_HZ  50
#define SERVO_RES_BITS 16
#define LED_FREQ_HZ    1000
#define LED_RES_BITS   10

// ---------------------------------------------------------------------------
// Enlace serie (sección 8.1 y 8.6)
// ---------------------------------------------------------------------------
#define SERIAL_BAUDIOS     115200
#define TRAMA_MAX          64      // bytes, sin contar el '\n'
#define T_VIGILANCIA_MS    5000    // sin tramas válidas -> PARADA + "SIN ENLACE"
#define CACHE_SEQ          8       // respuestas recordadas para descartar duplicados
#define VENTANA_DUPLICADO_MS 15000 // un SEQ repetido dentro de esta ventana es un reintento

// Tiempos máximos de los movimientos (tabla 8.3)
#define T_MAX_HOME_MS      3000
#define T_MAX_VOLTEO_MS    10000

// Rangos de argumentos
#define RETRY_EXTRA_MAX    2000    // pasos extra de arco aceptados por RETRY
#define MOVE_PASOS_MAX     4096    // MOVE,M: hasta una vuelta del rodillo por orden (medios pasos)
#define T_MAX_MANUAL_MS    28000   // tope de una orden MOVE (el límite real se calcula por orden)
#define SPIN_MS_MAX        10000   // SPIN: giro máximo por orden del servo de 360° del brazo

// Motor 28BYJ-48 en medio paso: 4096 pasos por vuelta del eje de salida
#define PASOS_POR_VUELTA   4096

// Códigos de error (tabla 8.5)
enum CodigoError : uint8_t {
  E_NINGUNO = 0,
  E01_CHECKSUM = 1,
  E02_DESCONOCIDO = 2,
  E03_OCUPADO = 3,
  E04_TIEMPO = 4,
  E05_RANGO = 5,
  E06_PARADA = 6,
  E07_REINICIO = 7,
};
