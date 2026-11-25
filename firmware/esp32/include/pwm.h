// PWM por hardware (periférico LEDC), compatible con Arduino-ESP32 2.x y 3.x
#pragma once
#include <Arduino.h>

void pwmIniciar(uint8_t pin, uint8_t canal, uint32_t frecuencia, uint8_t bits);
void pwmEscribir(uint8_t pin, uint8_t canal, uint32_t ciclo);

// Servo: microsegundos de pulso -> ciclo de 16 bits a 50 Hz
uint32_t servoCicloDesdeMicros(uint32_t micros);
