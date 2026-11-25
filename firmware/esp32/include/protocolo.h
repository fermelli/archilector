// Protocolo P1 (sección 8.2): $SEQ,CMD[,ARG1[,ARG2[,ARG3]]]*CS\n
// CS = XOR en hexadecimal (2 dígitos) de todos los bytes entre '$' y '*'.
#pragma once
#include <Arduino.h>

#define MAX_ARGS 3
#define MAX_LARGO_ARG 48

struct Trama {
  uint8_t seq;
  char cmd[8];
  char args[MAX_ARGS][MAX_LARGO_ARG];
  uint8_t nargs;
};

enum ResultadoTrama : uint8_t {
  TRAMA_OK,
  TRAMA_DESCARTAR,  // no empieza con '$' o supera 64 bytes: se ignora en silencio
  TRAMA_CHECKSUM,   // NAK,E01
  TRAMA_FORMATO,    // NAK,E02
};

uint8_t checksumXor(const char* datos, size_t largo);

// Analiza una línea sin el '\n'. El último argumento conserva las comas que
// contenga (útil para el texto de LCD).
ResultadoTrama analizarTrama(const char* linea, Trama& t);

// Extrae el SEQ de una trama aunque su suma sea incorrecta (para el NAK,E01).
uint8_t seqAproximado(const char* linea);

// Escribe "$seq,cuerpo*CS\n" en buf. Devuelve el largo o 0 si no cabe.
size_t armarTrama(char* buf, size_t cap, uint8_t seq, const char* cuerpo);

// Envío seguro desde cualquier tarea. Si seq != 0, la respuesta queda en la
// caché para reenviarla si llega otra vez el mismo SEQ (sección 8.6).
void protocoloIniciar();
void enviar(uint8_t seq, const char* cuerpo);
void enviarf(uint8_t seq, const char* formato, ...);
bool respuestaEnCache(uint8_t seq, char* cuerpo, size_t cap);
