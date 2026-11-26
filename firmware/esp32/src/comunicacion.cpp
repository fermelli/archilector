// Atiende los comandos de la tabla 8.3 y aplica las reglas de la sección 8.6
#include "comunicacion.h"
#include "calibracion.h"
#include "config.h"
#include "estado.h"
#include "interfaz.h"
#include "movimiento.h"
#include "protocolo.h"

static uint32_t ultimaValida = 0;
static bool vigilanciaArmada = false;  // se arma con la primera trama válida

static bool leerEntero(const char* s, long& valor) {
  if (!s || !*s) return false;
  char* fin = nullptr;
  valor = strtol(s, &fin, 10);
  return fin && *fin == '\0';
}

static void nak(uint8_t seq, uint8_t codigo) { enviarf(seq, "NAK,E%02u", codigo); }

static bool esMovimiento(const char* cmd) {
  return !strcmp(cmd, "HOME") || !strcmp(cmd, "TURN") || !strcmp(cmd, "RETRY") || !strcmp(cmd, "BACK") ||
         !strcmp(cmd, "MOVE") || !strcmp(cmd, "SPIN");
}

static void ordenMovimiento(const Trama& t) {
  Estado e = estadoActual();
  if (e == MOVIENDO || e == ARRANQUE) return nak(t.seq, E03_OCUPADO);

  Orden o{ORDEN_HOME, t.seq, 0};
  if (!strcmp(t.cmd, "HOME")) {
    o.tipo = ORDEN_HOME;  // HOME sale de FALLA o PARADA
  } else {
    if (e == PARADA || e == FALLA) return nak(t.seq, E06_PARADA);
    if (!strcmp(t.cmd, "TURN")) {
      o.tipo = ORDEN_TURN;
    } else if (!strcmp(t.cmd, "RETRY")) {
      long extra = 0;
      if (t.nargs != 1 || !leerEntero(t.args[0], extra) || extra < 0 || extra > RETRY_EXTRA_MAX)
        return nak(t.seq, E05_RANGO);
      o.tipo = ORDEN_RETRY;
      o.extra = (int32_t)extra;
    } else if (!strcmp(t.cmd, "MOVE")) {
      // Control manual de una pieza: MOVE,B,<0-180> · MOVE,R,<0-180> · MOVE,M,<±medios pasos>
      long valor = 0;
      if (t.nargs != 2 || strlen(t.args[0]) != 1 || !leerEntero(t.args[1], valor)) return nak(t.seq, E05_RANGO);
      char p = t.args[0][0];
      bool ok = (p == 'B' || p == 'R') ? (valor >= 0 && valor <= 180)
              : (p == 'M')             ? (valor >= -MOVE_PASOS_MAX && valor <= MOVE_PASOS_MAX)
                                       : false;
      if (!ok) return nak(t.seq, E05_RANGO);
      o.tipo = ORDEN_MOVER;
      o.pieza = p;
      o.extra = (int32_t)valor;
    } else if (!strcmp(t.cmd, "SPIN")) {
      // Servo de 360° en el brazo: SPIN,<señal 0-180>,<ms>,<quieto 0-180>. Gira con esa señal
      // durante ms (cronometrado aquí, no en la red) y vuelve a la señal de quieto.
      long senal = 0, ms = 0, quieto = 0;
      if (t.nargs != 3 || !leerEntero(t.args[0], senal) || !leerEntero(t.args[1], ms) ||
          !leerEntero(t.args[2], quieto) || senal < 0 || senal > 180 || ms < 1 || ms > SPIN_MS_MAX ||
          quieto < 0 || quieto > 180)
        return nak(t.seq, E05_RANGO);
      o.tipo = ORDEN_GIRO;
      o.extra = (int32_t)senal;
      o.ms = (int32_t)ms;
      o.quieto = (int32_t)quieto;
    } else {
      // BACK necesita girar el rodillo en reversa: imposible si tiene embrague (T3.0)
      if (cal.embrague) return nak(t.seq, E05_RANGO);
      o.tipo = ORDEN_BACK;
    }
  }

  g_abortar = false;
  fijarEstado(MOVIENDO, false);
  enviar(t.seq, "ACK");  // primero ACK, después DONE o FAIL
  if (o.tipo == ORDEN_HOME) lcdMostrar("VOLVIENDO", "A REPOSO");
  if (!movimientoEncolar(o)) {
    fijarEstado(FALLA, false);
    enviar(t.seq, "FAIL,E03");
  }
}

static void procesar(const Trama& t) {
  const char* c = t.cmd;

  if (!strcmp(c, "PING")) return enviarf(t.seq, "PONG,%s,%s", FW_VERSION, PROTO_VERSION);
  if (!strcmp(c, "STATUS"))
    return enviarf(t.seq, "STATE,%s,E%02u", nombreEstado(estadoActual()), ultimoError());
  if (!strcmp(c, "STOP")) {
    entrarParada("PARADA", false);
    return enviar(t.seq, "STATE,PARADA");
  }
  if (esMovimiento(c)) return ordenMovimiento(t);

  if (!strcmp(c, "LIGHT")) {
    long v;
    if (t.nargs != 1 || !leerEntero(t.args[0], v) || v < 0 || v > 100) return nak(t.seq, E05_RANGO);
    luzPorcentaje((uint8_t)v);
    return enviar(t.seq, "ACK");
  }
  if (!strcmp(c, "LCD")) {
    long linea;
    if (t.nargs < 2 || !leerEntero(t.args[0], linea) || linea < 1 || linea > 2) return nak(t.seq, E05_RANGO);
    char texto[MAX_LARGO_ARG * 2];
    if (t.nargs == 3) {
      snprintf(texto, sizeof(texto), "%s,%s", t.args[1], t.args[2]);
    } else {
      snprintf(texto, sizeof(texto), "%s", t.args[1]);
    }
    if (strlen(texto) > 16) return nak(t.seq, E05_RANGO);
    lcdLinea((uint8_t)linea, texto);
    return enviar(t.seq, "ACK");
  }
  if (!strcmp(c, "BEEP")) {
    long p;
    if (t.nargs != 1 || !leerEntero(t.args[0], p) || p < 1 || p > 3) return nak(t.seq, E05_RANGO);
    buzzerPatron((uint8_t)p);
    return enviar(t.seq, "ACK");
  }
  if (!strcmp(c, "FREE")) {
    // Corta los pulsos de un servo: un servo de 360° se detiene; uno de 180° queda suelto.
    // Se acepta también en PARADA (es una acción de seguridad), no durante un movimiento.
    // Con un segundo argumento (FREE,B,92) deja el servo fijo en esa señal, sin rampa: es como se
    // detiene un servo de 360° que, sin pulsos, sigue con la última orden.
    if (t.nargs < 1 || t.nargs > 2 || strlen(t.args[0]) != 1 || (t.args[0][0] != 'B' && t.args[0][0] != 'R'))
      return nak(t.seq, E05_RANGO);
    long senal = -1;
    if (t.nargs == 2 && (!leerEntero(t.args[1], senal) || senal < 0 || senal > 180)) return nak(t.seq, E05_RANGO);
    if (estadoActual() == MOVIENDO) return nak(t.seq, E03_OCUPADO);
    servoSoltar(t.args[0][0], (int)senal);
    return enviar(t.seq, "ACK");
  }
  if (!strcmp(c, "CFG")) {
    long v;
    if (estadoActual() == MOVIENDO) return nak(t.seq, E03_OCUPADO);
    if (t.nargs != 2 || !leerEntero(t.args[1], v)) return nak(t.seq, E05_RANGO);
    uint8_t r = calibracionFijar(t.args[0], v);
    if (r) return nak(t.seq, r);
    return enviar(t.seq, "ACK");
  }
  nak(t.seq, E02_DESCONOCIDO);
}

static void procesarLinea(const char* linea) {
  Trama t;
  switch (analizarTrama(linea, t)) {
    case TRAMA_DESCARTAR: return;
    case TRAMA_CHECKSUM: return nak(seqAproximado(linea), E01_CHECKSUM);
    case TRAMA_FORMATO: return nak(seqAproximado(linea), E02_DESCONOCIDO);
    case TRAMA_OK: break;
  }
  ultimaValida = millis();
  vigilanciaArmada = true;

  // Orden repetida (la Pi no recibió el ACK): se reenvía la respuesta guardada
  // sin repetir la acción. Así un reintento nunca provoca un volteo doble.
  char guardada[TRAMA_MAX];
  if (t.seq != 0 && respuestaEnCache(t.seq, guardada, sizeof(guardada))) {
    enviar(t.seq, guardada);
    return;
  }
  procesar(t);
}

void comunicacionTarea(void*) {
  char linea[TRAMA_MAX + 1];
  size_t largo = 0;
  bool desbordada = false;

  for (;;) {
    bool hubo = false;
    while (Serial.available()) {
      hubo = true;
      char ch = (char)Serial.read();
      if (ch == '\r') continue;
      if (ch == '\n') {
        if (!desbordada && largo > 0) {
          linea[largo] = '\0';
          procesarLinea(linea);
        }
        largo = 0;
        desbordada = false;
        continue;
      }
      if (largo >= TRAMA_MAX) {
        desbordada = true;  // más de 64 bytes: se descarta la trama completa
      } else {
        linea[largo++] = ch;
      }
    }

    // Vigilancia (8.6): 5 s sin tramas válidas -> PARADA y "SIN ENLACE".
    // Se arma con la primera trama, para no detenerse mientras la Pi arranca.
    if (vigilanciaArmada && millis() - ultimaValida > T_VIGILANCIA_MS && estadoActual() != PARADA) {
      entrarParada("SIN ENLACE", true);
    }

    if (!hubo) vTaskDelay(pdMS_TO_TICKS(1));
  }
}
