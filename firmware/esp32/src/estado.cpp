#include "estado.h"
#include "config.h"
#include "interfaz.h"
#include "movimiento.h"
#include "protocolo.h"

volatile bool g_abortar = false;
static volatile Estado estado = ARRANQUE;
static volatile uint8_t error = E_NINGUNO;

const char* nombreEstado(Estado e) {
  switch (e) {
    case ARRANQUE: return "ARRANQUE";
    case REPOSO: return "REPOSO";
    case MOVIENDO: return "MOVIENDO";
    case FALLA: return "FALLA";
    case PARADA: return "PARADA";
  }
  return "?";
}

Estado estadoActual() { return estado; }

void fijarEstado(Estado e, bool notificar) {
  estado = e;
  if (notificar) enviarf(0, "EVT,STATE,%s", nombreEstado(e));
}

uint8_t ultimoError() { return error; }
void fijarError(uint8_t codigo) { error = codigo; }

void entrarParada(const char* linea1, bool notificar) {
  g_abortar = true;          // el movimiento en curso sale en su próximo paso (< 10 ms)
  motorApagar();             // "motor paso a paso apagado" (figura 8.3)
  fijarEstado(PARADA, notificar);
  lcdMostrar(linea1, "A=REANUDAR");
  buzzerPatron(2);           // 1 pitido largo (tabla 7.3)
}
