// Controlador mínimo de LCD 16x2 HD44780 con mochila PCF8574 (I2C, a través del BSS138)
#pragma once
#include <Arduino.h>

class LcdI2C {
 public:
  // Busca la LCD en 0x27 y luego en 0x3F (prueba T2.4). Devuelve false si no responde.
  bool iniciar();
  bool presente() const { return direccion_ != 0; }
  uint8_t direccion() const { return direccion_; }
  void limpiar();
  // Escribe la línea completa (0 o 1), rellena con espacios hasta 16 caracteres.
  void escribirLinea(uint8_t fila, const char* texto);

 private:
  void enviarNibble(uint8_t nibble, uint8_t modo);
  void enviarByte(uint8_t valor, uint8_t modo);
  void comando(uint8_t c);
  void escribirExpansor(uint8_t dato);
  uint8_t direccion_ = 0;
};
