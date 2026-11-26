// Secuencias de volteo de la alternativa B (figura 8.2 y tabla 5 del Diseño).
// Toda la secuencia corre aquí, en el núcleo 1, sin intervención de la Pi.
#include "movimiento.h"
#include "calibracion.h"
#include "config.h"
#include "estado.h"
#include "interfaz.h"
#include "protocolo.h"
#include "pwm.h"

static QueueHandle_t colaOrdenes = nullptr;

// Ángulo comandado actual de cada servo (no hay realimentación de posición)
static float angBrazo = 90;
static float angBarra = 0;

// Medio paso del 28BYJ-48: dos bobinas activas en los pasos pares (tabla 3.3)
static const uint8_t SECUENCIA[8][4] = {
    {1, 0, 0, 0}, {1, 1, 0, 0}, {0, 1, 0, 0}, {0, 1, 1, 0},
    {0, 0, 1, 0}, {0, 0, 1, 1}, {0, 0, 0, 1}, {1, 0, 0, 1},
};
static int8_t fase = 0;

// ---------------------------------------------------------------------------
// Contexto de una orden: controla la parada y el tiempo máximo
// ---------------------------------------------------------------------------
struct Contexto {
  uint32_t inicio;
  uint32_t limite;
};

// 0 si se puede seguir; E06 si hubo parada; E04 si se excedió el tiempo
static uint8_t revisar(const Contexto& c) {
  if (g_abortar) return E06_PARADA;
  if (millis() - c.inicio > c.limite) return E04_TIEMPO;
  return E_NINGUNO;
}

static void escribirServo(uint8_t pin, uint8_t canal, float angulo) {
  if (angulo < 0) angulo = 0;
  if (angulo > 180) angulo = 180;
  uint32_t us = cal.pulsoMin + (uint32_t)((cal.pulsoMax - cal.pulsoMin) * angulo / 180.0f);
  pwmEscribir(pin, canal, servoCicloDesdeMicros(us));
}

// Rampa: avanza cada 5 ms lo que corresponde a la velocidad en °/s (2.2: el
// MG996R se mueve en rampa para no superar el pico de corriente ni el torque).
static uint8_t rampaServo(uint8_t pin, uint8_t canal, float& actual, int destino, int velocidad,
                          const Contexto& c) {
  const uint32_t PERIODO_MS = 5;
  const float paso = velocidad * PERIODO_MS / 1000.0f;
  while (fabsf(actual - destino) > 0.01f) {
    uint8_t r = revisar(c);
    if (r) return r;
    float diferencia = destino - actual;
    if (fabsf(diferencia) <= paso) {
      actual = (float)destino;
    } else {
      actual += (diferencia > 0 ? paso : -paso);
    }
    escribirServo(pin, canal, actual);
    vTaskDelay(pdMS_TO_TICKS(PERIODO_MS));
  }
  return E_NINGUNO;
}

// Servo sin pulsos (FREE): queda suelto. La próxima orden lo lleva directo al destino,
// sin rampa desde el último ángulo (en un servo de 360° esa rampa sería un giro).
static volatile bool brazoSuelto = false;
static volatile bool barraSuelta = false;

static uint8_t moverServo(uint8_t pin, uint8_t canal, float& actual, volatile bool& suelto, int destino,
                          int velocidad, const Contexto& c) {
  if (suelto) {
    suelto = false;
    actual = (float)destino;
    escribirServo(pin, canal, actual);
    return revisar(c);
  }
  return rampaServo(pin, canal, actual, destino, velocidad, c);
}

// --- Brazo con servo de 360° usado como si fuera de 180° (BRAZO_360 = 1) ---------------
// No hay sensor: la posición se estima. Para ir a un ángulo, el servo gira hacia ese lado
// el tiempo calibrado (MS90_MAS o MS90_MENOS por cada 90°) y vuelve a la señal de quieto.
static float posBrazo = 90;  // posición estimada del brazo (°)

static uint8_t pausa(uint32_t ms, const Contexto& c);

static uint8_t moverBrazo360(int destino, const Contexto& c) {
  const float delta = destino - posBrazo;
  brazoSuelto = false;
  angBrazo = (float)cal.quietoBrazo;
  if (fabsf(delta) < 0.5f) {
    escribirServo(PIN_SERVO_BRAZO, CANAL_BRAZO, angBrazo);
    posBrazo = (float)destino;
    return revisar(c);
  }
  const int sentido = delta > 0 ? 1 : -1;
  // El servo pierde MS_ARRANQUE en empezar a girar; el resto del tiempo es proporcional al
  // ángulo. MS90_* es el tiempo total de un giro de 90°, arranque incluido.
  const float ms90 = (float)(sentido > 0 ? cal.ms90Mas : cal.ms90Menos);
  const float arranque = fminf((float)cal.msArranque, ms90 - 1.0f);
  const uint32_t ms = (uint32_t)(arranque + fabsf(delta) / 90.0f * (ms90 - arranque));
  int senal = cal.quietoBrazo + sentido * cal.dirBrazo * cal.vel360;
  senal = senal < 0 ? 0 : (senal > 180 ? 180 : senal);
  escribirServo(PIN_SERVO_BRAZO, CANAL_BRAZO, (float)senal);
  const uint32_t t0 = millis();
  const uint8_t r = pausa(ms, c);
  uint32_t hecho = millis() - t0;
  if (hecho > ms) hecho = ms;
  escribirServo(PIN_SERVO_BRAZO, CANAL_BRAZO, angBrazo);  // quieto
  // Si una parada lo cortó a mitad, se estima cuánto alcanzó a girar (descontando el arranque)
  float fraccion = ms > arranque ? ((float)hecho - arranque) / ((float)ms - arranque) : 1.0f;
  fraccion = fraccion < 0 ? 0 : (fraccion > 1 ? 1 : fraccion);
  posBrazo = r ? posBrazo + delta * fraccion : (float)destino;
  return r;
}

static uint8_t moverBrazo(int destino, const Contexto& c) {
  if (cal.brazo360) return moverBrazo360(destino, c);
  return moverServo(PIN_SERVO_BRAZO, CANAL_BRAZO, angBrazo, brazoSuelto, destino, cal.velGiro, c);
}

int brazoPosicion() { return (int)lroundf(cal.brazo360 ? posBrazo : angBrazo); }

void brazoFijarPosicion(int angulo) { posBrazo = (float)angulo; }

void brazoModoAplicar() {
  // Al activar el modo 360° (o cambiar su quieto) el servo queda quieto; al volver al de
  // posición, se ordena el ángulo estimado para que no salte.
  brazoSuelto = false;
  if (cal.brazo360) {
    angBrazo = (float)cal.quietoBrazo;
  } else {
    angBrazo = posBrazo;
  }
  escribirServo(PIN_SERVO_BRAZO, CANAL_BRAZO, angBrazo);
}

static uint8_t moverBarra(int destino, const Contexto& c) {
  return moverServo(PIN_SERVO_BARRA, CANAL_BARRA, angBarra, barraSuelta, destino, cal.velBarra, c);
}

void servoSoltar(char pieza, int senal) {
  const bool brazo = pieza == 'B';
  const uint8_t pin = brazo ? PIN_SERVO_BRAZO : PIN_SERVO_BARRA;
  const uint8_t canal = brazo ? CANAL_BRAZO : CANAL_BARRA;
  if (senal < 0) {
    (brazo ? brazoSuelto : barraSuelta) = true;
    pwmEscribir(pin, canal, 0);
    return;
  }
  (brazo ? brazoSuelto : barraSuelta) = false;
  (brazo ? angBrazo : angBarra) = (float)senal;
  escribirServo(pin, canal, (float)senal);
}

static void aplicarFase() {
  for (int i = 0; i < 4; i++) digitalWrite(PIN_MOTOR[i], SECUENCIA[fase][i]);
}

void motorApagar() {
  for (int i = 0; i < 4; i++) digitalWrite(PIN_MOTOR[i], LOW);
}

// Gira el rodillo n medios pasos; sentido 1 = hacia el lomo (según DIR_RODILLO)
static uint8_t girarRodillo(int32_t pasos, int sentido, const Contexto& c) {
  const int dir = sentido * cal.dirRodillo;
  const uint32_t periodoUs = 1000000UL / (uint32_t)cal.velRodillo;
  uint32_t siguiente = micros();
  for (int32_t i = 0; i < pasos; i++) {
    uint8_t r = revisar(c);
    if (r) {
      motorApagar();
      return r;
    }
    fase = (int8_t)((fase + dir + 8) % 8);
    aplicarFase();
    siguiente += periodoUs;
    // Espera activa corta; cede la CPU cada 32 pasos para no acaparar el núcleo
    while ((int32_t)(micros() - siguiente) < 0) {
    }
    if ((i & 31) == 31) vTaskDelay(1);
  }
  return E_NINGUNO;
}

static uint8_t pausa(uint32_t ms, const Contexto& c) {
  uint32_t t0 = millis();
  while (millis() - t0 < ms) {
    uint8_t r = revisar(c);
    if (r) return r;
    vTaskDelay(pdMS_TO_TICKS(5));
  }
  return E_NINGUNO;
}

#define PASO(expr)          \
  do {                      \
    uint8_t _r = (expr);    \
    if (_r) return _r;      \
  } while (0)

// HOME: brazo a 90° y barra cerrada (tabla 8.3)
static uint8_t secuenciaHome(const Contexto& c) {
  motorApagar();
  PASO(moverBarra(cal.barraCerrada, c));
  PASO(moverBrazo(cal.angReposo, c));
  return E_NINGUNO;
}

// TURN / RETRY (figura 8.2): brazo 0° · rodillo arquea · barra abre ·
// giro a 180° · barra cierra · brazo a 90°
static uint8_t secuenciaVolteo(int32_t pasosExtra, const Contexto& c) {
  PASO(moverBarra(cal.barraCerrada, c));
  // 1. Apoyar: el rodillo descansa sobre la página derecha, cerca del borde
  PASO(moverBrazo(cal.angContacto, c));
  PASO(pausa(cal.tAsentar, c));
  // 2. Arquear: el rodillo gira hacia el lomo y levanta solo la hoja de arriba
  PASO(girarRodillo(cal.pasosArco + pasosExtra, 1, c));
  // 3. Montar: la barra se abre para recibir la hoja. El motor se desenergiza
  //    para que el rodillo gire libre cuando la hoja salga por debajo.
  PASO(moverBarra(cal.barraAbierta, c));
  motorApagar();
  // 4. Llevar: el brazo gira sobre el lomo hasta la página izquierda
  PASO(moverBrazo(cal.angDeposito, c));
  PASO(pausa(cal.tAsentar, c));
  // 5. Soltar y sujetar: la barra aplana la hoja; el brazo vuelve a reposo
  PASO(moverBarra(cal.barraCerrada, c));
  PASO(moverBrazo(cal.angReposo, c));
  return E_NINGUNO;
}

// BACK (tabla 8.3, "a validar en pruebas"): devuelve una hoja a la derecha.
// Brazo a 180°, rodillo en reversa, giro a 0° y vuelta a reposo.
static uint8_t secuenciaRegreso(const Contexto& c) {
  PASO(moverBarra(cal.barraAbierta, c));
  PASO(moverBrazo(cal.angDeposito, c));
  PASO(pausa(cal.tAsentar, c));
  PASO(girarRodillo(cal.pasosArco, -1, c));
  motorApagar();
  PASO(moverBrazo(cal.angContacto, c));
  PASO(pausa(cal.tAsentar, c));
  PASO(moverBarra(cal.barraCerrada, c));
  PASO(moverBrazo(cal.angReposo, c));
  return E_NINGUNO;
}

// MOVE (control manual desde el panel): mueve una sola pieza y la deja ahí
static uint8_t secuenciaManual(char pieza, int32_t valor, const Contexto& c) {
  if (pieza == 'B') return moverBrazo(valor, c);
  if (pieza == 'R') return moverBarra(valor, c);
  uint8_t r = girarRodillo(valor < 0 ? -valor : valor, valor < 0 ? -1 : 1, c);
  motorApagar();
  return r;
}

// SPIN: el brazo con un servo de 360° gira con una señal fija un tiempo exacto y vuelve al quieto.
// Si llega STOP en medio, también vuelve al quieto antes de salir.
static uint8_t secuenciaGiro(const Orden& o, const Contexto& c) {
  brazoSuelto = false;
  angBrazo = (float)o.extra;
  escribirServo(PIN_SERVO_BRAZO, CANAL_BRAZO, angBrazo);
  uint8_t r = pausa((uint32_t)o.ms, c);
  angBrazo = (float)o.quieto;
  escribirServo(PIN_SERVO_BRAZO, CANAL_BRAZO, angBrazo);
  return r;
}

// Tiempo máximo de una orden MOVE: lo que tarda a la velocidad calibrada + 1,5 s
static uint32_t limiteManual(const Orden& o) {
  float s;
  if (o.pieza == 'B' && cal.brazo360) {
    s = fabsf(posBrazo - o.extra) / 90.0f * (float)max(cal.ms90Mas, cal.ms90Menos) / 1000.0f;
  } else if (o.pieza == 'B') {
    s = fabsf(angBrazo - o.extra) / cal.velGiro;
  } else if (o.pieza == 'R') {
    s = fabsf(angBarra - o.extra) / cal.velBarra;
  } else {
    s = (float)labs(o.extra) / cal.velRodillo;
  }
  uint32_t ms = (uint32_t)(s * 1000) + 1500;
  return ms > T_MAX_MANUAL_MS ? T_MAX_MANUAL_MS : ms;
}

void movimientoIniciar() {
  for (int i = 0; i < 4; i++) {
    pinMode(PIN_MOTOR[i], OUTPUT);
    digitalWrite(PIN_MOTOR[i], LOW);
  }
  pwmIniciar(PIN_SERVO_BRAZO, CANAL_BRAZO, SERVO_FREQ_HZ, SERVO_RES_BITS);
  pwmIniciar(PIN_SERVO_BARRA, CANAL_BARRA, SERVO_FREQ_HZ, SERVO_RES_BITS);
  // Sin sensor de posición: se ordena directamente el reposo guardado en NVS.
  // Como cada secuencia termina en reposo, el salto al encender es mínimo (T2.2).
  // Con el brazo de 360° se supone que quedó en reposo y se le ordena quedarse quieto.
  posBrazo = (float)cal.angReposo;
  angBrazo = cal.brazo360 ? (float)cal.quietoBrazo : (float)cal.angReposo;
  angBarra = (float)cal.barraCerrada;
  escribirServo(PIN_SERVO_BRAZO, CANAL_BRAZO, angBrazo);
  escribirServo(PIN_SERVO_BARRA, CANAL_BARRA, angBarra);
  colaOrdenes = xQueueCreate(2, sizeof(Orden));
}

bool movimientoEncolar(const Orden& o) {
  return colaOrdenes && xQueueSend(colaOrdenes, &o, 0) == pdTRUE;
}

void movimientoTarea(void*) {
  Orden o;
  for (;;) {
    if (xQueueReceive(colaOrdenes, &o, portMAX_DELAY) != pdTRUE) continue;

    uint32_t limite = o.tipo == ORDEN_HOME    ? (uint32_t)T_MAX_HOME_MS
                    : o.tipo == ORDEN_MOVER   ? limiteManual(o)
                    : o.tipo == ORDEN_GIRO    ? (uint32_t)o.ms + 1500
                    // Volteo: 10 s para brazo y barra, más lo que tarda el arco del rodillo
                    // a la velocidad calibrada (un arco largo y lento puede llevar 15 s o más)
                                              : (uint32_t)T_MAX_VOLTEO_MS +
                                                    (uint32_t)((cal.pasosArco + o.extra) * 1000L / cal.velRodillo);
    Contexto c{millis(), limite};
    uint8_t r = E_NINGUNO;
    switch (o.tipo) {
      case ORDEN_HOME: r = secuenciaHome(c); break;
      case ORDEN_TURN: r = secuenciaVolteo(0, c); break;
      case ORDEN_RETRY: r = secuenciaVolteo(o.extra, c); break;
      case ORDEN_BACK: r = secuenciaRegreso(c); break;
      case ORDEN_MOVER: r = secuenciaManual(o.pieza, o.extra, c); break;
      case ORDEN_GIRO: r = secuenciaGiro(o, c); break;
    }
    motorApagar();
    if (r == E_NINGUNO && g_abortar) r = E06_PARADA;  // STOP llegó justo al terminar
    uint32_t duracion = millis() - c.inicio;

    if (r == E_NINGUNO) {
      fijarEstado(REPOSO, false);
      fijarError(E_NINGUNO);
      enviarf(o.seq, "DONE,%lu", (unsigned long)duracion);
      lcdMostrar("LISTO", "A=INICIAR");
    } else if (r == E04_TIEMPO) {
      fijarEstado(FALLA, false);
      fijarError(E04_TIEMPO);
      enviar(o.seq, "FAIL,E04");
      lcdMostrar("ERROR E04", "D=PARAR");
      buzzerPatron(3);
    } else {
      // Interrumpida por STOP, tecla D o pérdida de enlace: ya está en PARADA
      enviar(o.seq, "FAIL,E06");
    }
  }
}
