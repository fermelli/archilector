// Interfaz I1: recepción de órdenes de la Pi, respuestas y vigilancia del enlace
#pragma once
#include <Arduino.h>

void comunicacionTarea(void* parametro);  // núcleo 0
