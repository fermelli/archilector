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
  // Brazo con un servo de rotación continua (360°) usado como si fuera de 180°: el ángulo se
  // estima por tiempo de giro (sin sensor). Ver movimiento.cpp, moverBrazo360().
  int32_t brazo360;      // BRAZO_360      1 = servo de 360° emulado; 0 = servo de posición
  int32_t quietoBrazo;   // QUIETO_BRAZO   señal (°) con la que el servo de 360° no gira
  int32_t vel360;        // VEL_360        grados de señal desde el quieto mientras gira
  int32_t ms90Mas;       // MS90_MAS       ms para recorrer 90° hacia ángulos mayores
  int32_t ms90Menos;     // MS90_MENOS     ms para recorrer 90° hacia ángulos menores
  int32_t dirBrazo;      // DIR_BRAZO      1 o -1: lado de la señal que hace crecer el ángulo
  int32_t msArranque;    // MS_ARRANQUE    ms que el servo tarda en empezar a girar (incluidos en MS90_*)
};

extern Calibracion cal;

void calibracionCargar();
// Cambia un parámetro por su nombre y lo guarda en NVS.
// Devuelve E_NINGUNO o E05_RANGO (nombre desconocido o valor fuera de rango).
uint8_t calibracionFijar(const char* nombre, long valor);
// Lee un parámetro por su nombre (CFG,<NOMBRE> sin valor). false si no existe.
bool calibracionLeer(const char* nombre, long& valor);
// Escribe "NOMBRE=valor;..." en buf (para depuración por monitor serie)
void calibracionDescribir(char* buf, size_t cap);
