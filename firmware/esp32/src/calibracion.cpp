#include "calibracion.h"
#include <Preferences.h>
#include "config.h"

Calibracion cal;

struct Parametro {
  const char* nombre;  // también es la clave en NVS (máx. 15 caracteres)
  int32_t* destino;
  int32_t minimo;
  int32_t maximo;
  int32_t porDefecto;
};

// Valores iniciales de la tabla 5.5. PASOS_ARCO: 20 mm de avance con una goma
// de ~25 mm de diámetro -> 20 × 4096 / (π × 25) ≈ 1043; se ajusta en T3.2.
static const Parametro PARAMETROS[] = {
    {"ANG_REPOSO", &cal.angReposo, 0, 180, 90},
    {"ANG_CONTACTO", &cal.angContacto, 0, 180, 0},
    {"ANG_DEPOSITO", &cal.angDeposito, 0, 180, 180},
    {"PASOS_ARCO", &cal.pasosArco, 0, 8192, 1040},
    {"VEL_GIRO", &cal.velGiro, 10, 360, 120},
    {"BARRA_ABIERTA", &cal.barraAbierta, 0, 180, 70},
    {"BARRA_CERRADA", &cal.barraCerrada, 0, 180, 0},
    {"VEL_BARRA", &cal.velBarra, 10, 360, 180},
    {"VEL_RODILLO", &cal.velRodillo, 50, 1000, 600},
    {"DIR_RODILLO", &cal.dirRodillo, -1, 1, 1},
    {"EMBRAGUE", &cal.embrague, 0, 1, 0},
    {"T_ASENTAR", &cal.tAsentar, 0, 2000, 250},
    {"PULSO_MIN", &cal.pulsoMin, 400, 1200, 500},
    {"PULSO_MAX", &cal.pulsoMax, 1800, 2600, 2500},
    {"BRAZO_360", &cal.brazo360, 0, 1, 0},
    {"QUIETO_BRAZO", &cal.quietoBrazo, 0, 180, 90},
    {"VEL_360", &cal.vel360, 5, 90, 30},
    {"MS90_MAS", &cal.ms90Mas, 50, 10000, 600},
    {"MS90_MENOS", &cal.ms90Menos, 50, 10000, 600},
    {"DIR_BRAZO", &cal.dirBrazo, -1, 1, 1},
    {"MS_ARRANQUE", &cal.msArranque, 0, 500, 0},
};
static const size_t N_PARAMETROS = sizeof(PARAMETROS) / sizeof(PARAMETROS[0]);

static Preferences nvs;

void calibracionCargar() {
  nvs.begin("archilector", true);
  for (size_t i = 0; i < N_PARAMETROS; i++) {
    const Parametro& p = PARAMETROS[i];
    int32_t v = nvs.getInt(p.nombre, p.porDefecto);
    if (v < p.minimo || v > p.maximo) v = p.porDefecto;
    *p.destino = v;
  }
  nvs.end();
}

uint8_t calibracionFijar(const char* nombre, long valor) {
  for (size_t i = 0; i < N_PARAMETROS; i++) {
    const Parametro& p = PARAMETROS[i];
    if (strcmp(p.nombre, nombre) != 0) continue;
    if (valor < p.minimo || valor > p.maximo) return E05_RANGO;
    if ((strcmp(nombre, "DIR_RODILLO") == 0 || strcmp(nombre, "DIR_BRAZO") == 0) && valor == 0) return E05_RANGO;
    *p.destino = (int32_t)valor;
    nvs.begin("archilector", false);
    nvs.putInt(p.nombre, (int32_t)valor);
    nvs.end();
    return E_NINGUNO;
  }
  return E05_RANGO;
}

bool calibracionLeer(const char* nombre, long& valor) {
  for (size_t i = 0; i < N_PARAMETROS; i++) {
    if (strcmp(PARAMETROS[i].nombre, nombre) == 0) {
      valor = *PARAMETROS[i].destino;
      return true;
    }
  }
  return false;
}

void calibracionDescribir(char* buf, size_t cap) {
  size_t n = 0;
  buf[0] = '\0';
  for (size_t i = 0; i < N_PARAMETROS && n < cap; i++) {
    n += snprintf(buf + n, cap - n, "%s=%ld;", PARAMETROS[i].nombre, (long)*PARAMETROS[i].destino);
  }
}
