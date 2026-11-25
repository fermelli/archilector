#include "pwm.h"
#include "config.h"

void pwmIniciar(uint8_t pin, uint8_t canal, uint32_t frecuencia, uint8_t bits) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  (void)canal;
  ledcAttachChannel(pin, frecuencia, bits, canal);
#else
  ledcSetup(canal, frecuencia, bits);
  ledcAttachPin(pin, canal);
#endif
}

void pwmEscribir(uint8_t pin, uint8_t canal, uint32_t ciclo) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  (void)canal;
  ledcWrite(pin, ciclo);
#else
  (void)pin;
  ledcWrite(canal, ciclo);
#endif
}

uint32_t servoCicloDesdeMicros(uint32_t micros) {
  const uint32_t periodoUs = 1000000UL / SERVO_FREQ_HZ;  // 20 000 µs
  const uint32_t maximo = (1UL << SERVO_RES_BITS) - 1;
  return (uint32_t)((uint64_t)micros * maximo / periodoUs);
}
