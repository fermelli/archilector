// Bloque B6 del lado del ESP32: teclado 4x4, LCD 16x2, buzzer y tira LED
#pragma once
#include <Arduino.h>

void interfazIniciar();
void interfazTarea(void* parametro);  // núcleo 0: barrido del teclado, LCD y buzzer

// Seguras desde cualquier tarea (la LCD se dibuja en la tarea de interfaz)
void lcdMostrar(const char* linea1, const char* linea2);
void lcdLinea(uint8_t linea, const char* texto);  // linea 1 o 2
void buzzerPatron(uint8_t patron);                 // 1 corto · 2 largo · 3 tres largos
void luzPorcentaje(uint8_t porcentaje);            // 0..100
bool lcdPresente();
