// Mecanismo B: brazo (MG996R), rodillos (28BYJ-48 + ULN2003) y barra (SG90)
#pragma once
#include <Arduino.h>

enum TipoOrden : uint8_t { ORDEN_HOME, ORDEN_TURN, ORDEN_RETRY, ORDEN_BACK, ORDEN_MOVER, ORDEN_GIRO };

struct Orden {
  TipoOrden tipo;
  uint8_t seq;
  int32_t extra;   // pasos extra de arco (RETRY) · ángulo o medios pasos (MOVE) · señal (SPIN)
  char pieza;      // MOVE: 'B' brazo, 'R' barra, 'M' motor del rodillo
  int32_t ms;      // SPIN: duración del giro
  int32_t quieto;  // SPIN: señal en la que el servo de 360° queda quieto
};

void movimientoIniciar();             // PWM y servos directamente en reposo
bool movimientoEncolar(const Orden& o);
void movimientoTarea(void* parametro);  // núcleo 1, prioridad máxima
void motorApagar();                     // desenergiza las cuatro fases
// FREE: 'B' brazo o 'R' barra sin pulsos (queda suelto); con senal >= 0, fijo en esa señal sin rampa
void servoSoltar(char pieza, int senal = -1);
