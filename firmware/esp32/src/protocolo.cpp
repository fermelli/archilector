#include "protocolo.h"
#include <stdarg.h>
#include "config.h"

static SemaphoreHandle_t mutexSerie = nullptr;

struct EntradaCache {
  bool usada;
  uint8_t seq;
  uint32_t recibida;  // millis() de la primera respuesta con este SEQ
  char cuerpo[TRAMA_MAX];
};
static EntradaCache cache[CACHE_SEQ];
static uint8_t proximaEntrada = 0;
static portMUX_TYPE muxCache = portMUX_INITIALIZER_UNLOCKED;

uint8_t checksumXor(const char* datos, size_t largo) {
  uint8_t cs = 0;
  for (size_t i = 0; i < largo; i++) cs ^= (uint8_t)datos[i];
  return cs;
}

static int valorHex(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

static bool esNumero(const char* s, size_t n) {
  if (n == 0 || n > 3) return false;
  for (size_t i = 0; i < n; i++)
    if (s[i] < '0' || s[i] > '9') return false;
  return true;
}

uint8_t seqAproximado(const char* linea) {
  if (linea[0] != '$') return 0;
  int v = 0;
  for (size_t i = 1; i < 4 && linea[i] >= '0' && linea[i] <= '9'; i++) v = v * 10 + (linea[i] - '0');
  return (v >= 0 && v <= 255) ? (uint8_t)v : 0;
}

ResultadoTrama analizarTrama(const char* linea, Trama& t) {
  size_t largo = strlen(linea);
  if (largo == 0 || largo > TRAMA_MAX || linea[0] != '$') return TRAMA_DESCARTAR;

  const char* asterisco = strrchr(linea, '*');
  if (!asterisco || (size_t)(linea + largo - asterisco) != 3) return TRAMA_FORMATO;
  int alto = valorHex(asterisco[1]);
  int bajo = valorHex(asterisco[2]);
  if (alto < 0 || bajo < 0) return TRAMA_FORMATO;
  uint8_t esperado = (uint8_t)((alto << 4) | bajo);
  size_t largoCuerpo = (size_t)(asterisco - linea - 1);
  if (checksumXor(linea + 1, largoCuerpo) != esperado) return TRAMA_CHECKSUM;

  // Cuerpo: SEQ,CMD[,args]
  char cuerpo[TRAMA_MAX + 1];
  memcpy(cuerpo, linea + 1, largoCuerpo);
  cuerpo[largoCuerpo] = '\0';

  char* coma = strchr(cuerpo, ',');
  if (!coma) return TRAMA_FORMATO;
  *coma = '\0';
  if (!esNumero(cuerpo, strlen(cuerpo))) return TRAMA_FORMATO;
  int seq = atoi(cuerpo);
  if (seq > 255) return TRAMA_FORMATO;
  t.seq = (uint8_t)seq;

  char* resto = coma + 1;
  char* siguiente = strchr(resto, ',');
  if (siguiente) *siguiente = '\0';
  size_t largoCmd = strlen(resto);
  if (largoCmd < 2 || largoCmd >= sizeof(t.cmd)) return TRAMA_FORMATO;
  strcpy(t.cmd, resto);

  t.nargs = 0;
  while (siguiente && t.nargs < MAX_ARGS) {
    char* arg = siguiente + 1;
    char* fin = (t.nargs < MAX_ARGS - 1) ? strchr(arg, ',') : nullptr;
    if (fin) *fin = '\0';
    strncpy(t.args[t.nargs], arg, MAX_LARGO_ARG - 1);
    t.args[t.nargs][MAX_LARGO_ARG - 1] = '\0';
    t.nargs++;
    siguiente = fin;
  }
  return TRAMA_OK;
}

size_t armarTrama(char* buf, size_t cap, uint8_t seq, const char* cuerpo) {
  char interior[TRAMA_MAX + 1];
  int n = snprintf(interior, sizeof(interior), "%u,%s", seq, cuerpo);
  if (n < 0 || (size_t)n >= sizeof(interior)) return 0;
  uint8_t cs = checksumXor(interior, (size_t)n);
  int total = snprintf(buf, cap, "$%s*%02X\n", interior, cs);
  if (total < 0 || (size_t)total >= cap) return 0;
  return (size_t)total;
}

void protocoloIniciar() {
  mutexSerie = xSemaphoreCreateMutex();
  for (auto& e : cache) e.usada = false;
}

static void guardarEnCache(uint8_t seq, const char* cuerpo) {
  portENTER_CRITICAL(&muxCache);
  // Si el SEQ ya está (p. ej. ACK y luego DONE de la misma orden), se actualiza.
  for (auto& e : cache) {
    if (e.usada && e.seq == seq) {
      strncpy(e.cuerpo, cuerpo, sizeof(e.cuerpo) - 1);
      e.cuerpo[sizeof(e.cuerpo) - 1] = '\0';
      portEXIT_CRITICAL(&muxCache);
      return;
    }
  }
  EntradaCache& e = cache[proximaEntrada];
  proximaEntrada = (proximaEntrada + 1) % CACHE_SEQ;
  e.usada = true;
  e.seq = seq;
  e.recibida = millis();
  strncpy(e.cuerpo, cuerpo, sizeof(e.cuerpo) - 1);
  e.cuerpo[sizeof(e.cuerpo) - 1] = '\0';
  portEXIT_CRITICAL(&muxCache);
}

bool respuestaEnCache(uint8_t seq, char* cuerpo, size_t cap) {
  bool hallada = false;
  portENTER_CRITICAL(&muxCache);
  for (auto& e : cache) {
    // Solo cuenta como duplicado dentro de la ventana: evita confundir una orden
    // nueva con una vieja si la Pi reinicia su contador de SEQ.
    if (e.usada && e.seq == seq && millis() - e.recibida < VENTANA_DUPLICADO_MS) {
      strncpy(cuerpo, e.cuerpo, cap - 1);
      cuerpo[cap - 1] = '\0';
      hallada = true;
      break;
    }
  }
  portEXIT_CRITICAL(&muxCache);
  return hallada;
}

void enviar(uint8_t seq, const char* cuerpo) {
  char buf[TRAMA_MAX + 8];
  size_t n = armarTrama(buf, sizeof(buf), seq, cuerpo);
  if (!n) return;
  // Los NAK no se guardan: la Pi reenvía la misma trama tras un NAK,E01 y
  // debe ejecutarse esa vez.
  if (seq != 0 && strncmp(cuerpo, "NAK", 3) != 0) guardarEnCache(seq, cuerpo);
  if (mutexSerie) xSemaphoreTake(mutexSerie, portMAX_DELAY);
  Serial.write((const uint8_t*)buf, n);
  if (mutexSerie) xSemaphoreGive(mutexSerie);
}

void enviarf(uint8_t seq, const char* formato, ...) {
  char cuerpo[TRAMA_MAX];
  va_list ap;
  va_start(ap, formato);
  vsnprintf(cuerpo, sizeof(cuerpo), formato, ap);
  va_end(ap);
  enviar(seq, cuerpo);
}
