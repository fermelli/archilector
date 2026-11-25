// Parámetros de calibración (tabla 5.5 del Documento Técnico), guardados en NVS
#pragma once
#include <Arduino.h>

struct Calibracion {
  int32_t angReposo;     // ANG_REPOSO     brazo vertical sobre el lomo (°)
  int32_t angContacto;   // ANG_CONTACTO   rodillo sobre la página derecha (°)
  int32_t angDeposito;   // ANG_DEPOSITO   brazo sobre la página izquierda (°)
  int32_t pasosArco;     // PASOS_ARCO     avance × 4096 / (π × D)
  int32_t velGiro;       // VEL_GIRO       rampa del MG996R (°/s)
  int32_t barraAbierta;  // BARRA_ABIERTA  ángulo del SG90 (°)
  int32_t barraCerrada;  // BARRA_CERRADA  ángulo del SG90 (°)
  int32_t velBarra;      // VEL_BARRA      rampa del SG90 (°/s)
  int32_t velRodillo;    // VEL_RODILLO    pasos por segundo del 28BYJ-48
  int32_t dirRodillo;    // DIR_RODILLO    1 o -1: sentido "hacia el lomo"
  int32_t embrague;      // EMBRAGUE       1 si los rodillos tienen embrague (T3.0): BACK queda deshabilitado
  int32_t tAsentar;      // T_ASENTAR      pausa tras apoyar o depositar (ms)
  int32_t pulsoMin;      // PULSO_MIN      µs para 0°
  int32_t pulsoMax;      // PULSO_MAX      µs para 180°
};

extern Calibracion cal;

void calibracionCargar();
// Cambia un parámetro por su nombre y lo guarda en NVS.
// Devuelve E_NINGUNO o E05_RANGO (nombre desconocido o valor fuera de rango).
uint8_t calibracionFijar(const char* nombre, long valor);
// Escribe "NOMBRE=valor;..." en buf (para depuración por monitor serie)
void calibracionDescribir(char* buf, size_t cap);
