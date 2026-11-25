// Máquina de estados del ESP32 (figura 8.3 del Documento Técnico)
#pragma once
#include <Arduino.h>

enum Estado : uint8_t { ARRANQUE, REPOSO, MOVIENDO, FALLA, PARADA };

const char* nombreEstado(Estado e);
Estado estadoActual();
// notificar = true envía EVT,STATE,<estado> (cambios "por su cuenta", SEQ 0)
void fijarEstado(Estado e, bool notificar);
uint8_t ultimoError();
void fijarError(uint8_t codigo);

// Bandera que revisan los movimientos en cada paso: STOP, tecla D o vigilancia
extern volatile bool g_abortar;

// Parada inmediata desde cualquier tarea: corta actuadores y pasa a PARADA.
void entrarParada(const char* linea1, bool notificar);
