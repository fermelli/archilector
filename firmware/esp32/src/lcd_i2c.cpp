#include "lcd_i2c.h"
#include <Wire.h>
#include "config.h"

// Mapa habitual de la mochila PCF8574: P0=RS, P1=RW, P2=E, P3=luz, P4..P7=D4..D7
static const uint8_t BIT_RS = 0x01;
static const uint8_t BIT_EN = 0x04;
static const uint8_t BIT_LUZ = 0x08;

static bool responde(uint8_t dir) {
  Wire.beginTransmission(dir);
  return Wire.endTransmission() == 0;
}

bool LcdI2C::iniciar() {
  Wire.begin(PIN_SDA, PIN_SCL, 100000);  // 100 kHz (tabla 2.1, interfaz I5)
  const uint8_t candidatas[] = {0x27, 0x3F};
  direccion_ = 0;
  for (uint8_t d : candidatas) {
    if (responde(d)) {
      direccion_ = d;
      break;
    }
  }
  if (!direccion_) return false;

  delay(50);
  // Secuencia de inicio en modo de 4 bits (hoja de datos HD44780)
  enviarNibble(0x30, 0);
  delayMicroseconds(4500);
  enviarNibble(0x30, 0);
  delayMicroseconds(4500);
  enviarNibble(0x30, 0);
  delayMicroseconds(150);
  enviarNibble(0x20, 0);
  comando(0x28);  // 4 bits, 2 líneas, 5x8
  comando(0x0C);  // pantalla encendida, sin cursor
  comando(0x06);  // avance a la derecha
  limpiar();
  return true;
}

void LcdI2C::limpiar() {
  if (!direccion_) return;
  comando(0x01);
  delayMicroseconds(2000);
}

void LcdI2C::escribirLinea(uint8_t fila, const char* texto) {
  if (!direccion_) return;
  comando(0x80 | (fila ? 0x40 : 0x00));
  bool fin = false;
  for (uint8_t i = 0; i < 16; i++) {
    char c = ' ';
    if (!fin && texto[i] != '\0') {
      c = texto[i];
    } else {
      fin = true;
    }
    enviarByte((uint8_t)c, BIT_RS);
  }
}

void LcdI2C::comando(uint8_t c) { enviarByte(c, 0); }

void LcdI2C::enviarByte(uint8_t valor, uint8_t modo) {
  enviarNibble(valor & 0xF0, modo);
  enviarNibble((valor << 4) & 0xF0, modo);
}

void LcdI2C::enviarNibble(uint8_t nibble, uint8_t modo) {
  uint8_t dato = nibble | modo | BIT_LUZ;
  escribirExpansor(dato | BIT_EN);
  delayMicroseconds(1);
  escribirExpansor(dato & ~BIT_EN);
  delayMicroseconds(50);
}

void LcdI2C::escribirExpansor(uint8_t dato) {
  Wire.beginTransmission(direccion_);
  Wire.write(dato);
  Wire.endTransmission();
}
