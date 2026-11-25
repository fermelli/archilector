// ArchiLector · firmware del ESP32 (Documento Técnico v2.2, bloque B2)
//
// Reparto de núcleos (sección 4.1):
//   núcleo 1 -> tarea de movimiento, prioridad máxima (secuencia de volteo)
//   núcleo 0 -> tarea de comunicación USB serie y tarea de interfaz
// La Raspberry Pi nunca mueve un actuador; el ESP32 nunca decide si una
// página quedó bien volteada.
#ifndef PRUEBA_SALIDAS

#include <Arduino.h>
#include <esp_system.h>
#include "calibracion.h"
#include "comunicacion.h"
#include "config.h"
#include "estado.h"
#include "interfaz.h"
#include "movimiento.h"
#include "protocolo.h"

static bool reinicioAnomalo() {
  switch (esp_reset_reason()) {
    case ESP_RST_BROWNOUT:
    case ESP_RST_PANIC:
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT:
      return true;
    default:
      return false;
  }
}

void setup() {
  // 1. Salidas en estado seguro antes que nada (prueba T2.2): ningún LED,
  //    buzzer ni bobina se activa durante el arranque.
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);
  for (int i = 0; i < 4; i++) {
    pinMode(PIN_MOTOR[i], OUTPUT);
    digitalWrite(PIN_MOTOR[i], LOW);
  }

  Serial.setRxBufferSize(512);
  Serial.begin(SERIAL_BAUDIOS);

  protocoloIniciar();
  calibracionCargar();
  interfazIniciar();
  movimientoIniciar();
  btStop();  // sin Bluetooth; el Wi-Fi nunca se inicia en operación (4.1)

  fijarEstado(ARRANQUE, false);
  if (reinicioAnomalo()) {
    // E07 (tabla 8.5): la Pi debe revisar la alimentación y enviar HOME
    fijarError(E07_REINICIO);
    enviar(0, "EVT,STATE,ARRANQUE,E07");
  }

  xTaskCreatePinnedToCore(movimientoTarea, "movimiento", 4096, nullptr, configMAX_PRIORITIES - 1, nullptr, 1);
  xTaskCreatePinnedToCore(comunicacionTarea, "comunicacion", 4096, nullptr, 5, nullptr, 0);
  xTaskCreatePinnedToCore(interfazTarea, "interfaz", 4096, nullptr, 3, nullptr, 0);

  // Tiempo para que los servos lleguen a reposo antes de aceptar movimientos
  delay(800);
  fijarEstado(REPOSO, true);
  lcdMostrar("LISTO", "A=INICIAR");
  buzzerPatron(1);
}

void loop() {
  vTaskDelete(nullptr);  // todo el trabajo está en las tareas de FreeRTOS
}

#endif  // PRUEBA_SALIDAS
