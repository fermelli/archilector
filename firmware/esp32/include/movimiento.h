// Mecanismo B: brazo (MG996R), rodillos (28BYJ-48 + ULN2003) y barra (SG90)
#pragma once
#include <Arduino.h>

enum TipoOrden : uint8_t { ORDEN_HOME, ORDEN_TURN, ORDEN_RETRY, ORDEN_BACK };

struct Orden {
  TipoOrden tipo;
  uint8_t seq;
  int32_t extra;  // pasos extra de arco (RETRY)
};

void movimientoIniciar();             // PWM y servos directamente en reposo
bool movimientoEncolar(const Orden& o);
void movimientoTarea(void* parametro);  // núcleo 1, prioridad máxima
void motorApagar();                     // desenergiza las cuatro fases
