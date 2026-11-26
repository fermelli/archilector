# ArchiLector · software v2.2 (alternativa B, rodillos Canon)

Software del robot que pasa solo las páginas de libros empastados, las digitaliza como PDF buscable
y las lee en voz alta. Implementa el **Documento de Diseño v2.3** y el **Documento Técnico de Armado y
Comunicación v2.3** con la alternativa B de cambio de página: dos rodillos Canon FB6-3405-000 en una
varilla M8, brazo con servo MG996R, motor 28BYJ-48 y barra de retención con SG90.

```
ArchiLector/
├─ docs/armado/        Guía de armado paso a paso con 12 esquemas SVG
├─ firmware/esp32/     ESP32 · PlatformIO + Arduino · tiempo real del volteo (bloque B2)
├─ pi/                 Raspberry Pi 4 · Python · visión, OCR, IA, voz, PDF y panel web (B4, B5, B6)
└─ entrenamiento/      Google Colab · verificador de volteo y clasificador de páginas (Bloque IV)
```

**¿Empezando desde cero?** Siga la [guía de armado](docs/armado/README.md): lista de compras,
esquemas eléctricos y mecánicos, orden de armado, pruebas y calibración.

La regla de reparto es la del Documento Técnico: **la Raspberry Pi nunca mueve un actuador y el ESP32
nunca decide si una página quedó bien volteada.**

---

## 1. Firmware del ESP32

```bash
cd firmware/esp32
pio run -t upload          # firmware principal
pio device monitor         # 115200 baudios
pio run -e prueba_salidas -t upload   # prueba T2.3 (activa cada salida; también T2.4 y T2.5)
```

| Módulo | Qué hace | Referencia |
|---|---|---|
| `config.h` | Pines, canales LEDC, tiempos, códigos de error | Técnico tabla 4.3 y 8.5 |
| `protocolo.cpp` | Trama `$SEQ,CMD,ARGS*CS\n`, XOR, caché de SEQ para descartar duplicados | Técnico 8.2 y 8.6 |
| `comunicacion.cpp` | Comandos PING, STATUS, HOME, TURN, RETRY, BACK, LIGHT, LCD, BEEP, STOP, CFG; vigilancia de 5 s | Técnico 8.3 y 8.6 |
| `estado.cpp` | ARRANQUE → REPOSO ⇄ MOVIENDO → FALLA / PARADA | Técnico figura 8.3 |
| `movimiento.cpp` | Secuencias del rodillo con servos en rampa y parada en < 10 ms | Técnico figura 8.2, Diseño tabla 5 |
| `interfaz.cpp`, `lcd_i2c.cpp` | Teclado 4×4, buzzer, tira LED; la LCD por PCF8574 es opcional (en v2.3 no se monta y el firmware sigue sin ella) | Técnico 7.2 y 7.3 |
| `calibracion.cpp` | Parámetros de la tabla 5.5 en NVS | Técnico 5.5 |

Tareas de FreeRTOS: movimiento en el núcleo 1 con prioridad máxima; comunicación e interfaz en el núcleo 0.
El firmware no usa librerías externas y compila con Arduino-ESP32 2.x y 3.x.

**Secuencia TURN:** barra cerrada → brazo a `ANG_CONTACTO` → el rodillo da `PASOS_ARCO` pasos hacia el lomo →
la barra se abre → motor desenergizado (rueda libre) → brazo a `ANG_DEPOSITO` → la barra se cierra → brazo a reposo.
`RETRY,n` es la misma secuencia con `n` pasos extra. `BACK` queda marcado como «a validar en pruebas» y se
deshabilita con `CFG,EMBRAGUE,1` si los rodillos tienen embrague (prueba T3.0).

### Decisiones donde los documentos no coinciden o no lo especifican

* **Pines:** se usa la tabla 4.3 del Documento Técnico (buzzer GPIO4, filas del teclado 13/16/17/23, LED GPIO27).
  La tabla 2b del Documento de Diseño tiene otra asignación y conviene actualizarla.
* **E07:** al arrancar tras un brownout, pánico o watchdog el ESP32 envía `$0,EVT,STATE,ARRANQUE,E07*CS`.
* **Vigilancia de 5 s:** se arma con la primera trama válida, para que el ESP32 no quede en PARADA mientras la Pi arranca.
* **Movimiento interrumpido** por STOP, tecla D o pérdida de enlace: responde `FAIL,E06` a la orden en curso.
* **Duplicados:** un SEQ repetido cuenta como reintento solo dentro de 15 s (la Pi además empieza con un SEQ al azar).
  Los NAK no se guardan, para que el reenvío tras un `NAK,E01` sí se ejecute.
* **Latido:** la Pi envía PING también durante los movimientos: un volteo dura unos 6 s y la vigilancia es de 5 s.
* **Frenar el brazo con la mano (TI.6):** los servos no informan su posición, así que solo se detecta si la secuencia
  supera los 10 s (`FAIL,E04`).

---

## 2. Software de la Raspberry Pi

### Instalación en la Pi (Raspberry Pi OS Lite 64 bits)

```bash
git clone <repositorio> ~/ArchiLector && cd ~/ArchiLector/pi
bash deploy/instalar.sh     # apt, zram, venv, Piper, Vosk, regla udev y servicio systemd
.venv/bin/python -m archilector prueba-enlace    # T2.1 y TC.1–TC.6 con el ESP32 conectado
sudo systemctl start archilector                 # panel en http://<pi>.local:8000
```

### Desarrollo en un PC, sin hardware

```bash
cd pi
python -m venv .venv && .venv/Scripts/pip install -e .[dev]     # en Linux: .venv/bin/pip
.venv/Scripts/python -m pytest                                    # 86 pruebas
.venv/Scripts/python -m archilector ejecutar --puerto sim:// --camara sim --sin-voz
```

`sim://` es un ESP32 simulado con la misma lógica que el firmware, y `--camara sim` fotografía un libro
simulado que el simulador "voltea". Se pueden provocar fallas en `config.yaml`:

```yaml
simulacion: {escala_tiempo: 0.2, prob_fallas: {no_pasa: 0.15, dos_hojas: 0.1, doblada: 0.05}}
```

### Cámara

| Fuente | Qué es |
|---|---|
| `auto` (por defecto) | La última elegida en el panel; si no, la cámara CSI; si no, la primera webcam |
| `pi` | Cámara V1.3 por CSI con Picamera2, 2592 × 1944 |
| `webcam`, `webcam:<n>` o `webcam:<ruta>` | Webcam USB (UVC) con OpenCV; en la Pi se identifica por `/dev/v4l/by-id/usb-…` |
| `sim`, `carpeta:<ruta>` | Libro simulado o imágenes de una carpeta, para desarrollo |

Se elige en `config.yaml` (`camara.fuente`), con `--camara` o en **Panel → Calibración → Cámara**, que
además permite cambiarla sin reiniciar y sacar una foto de prueba. Cada cámara puede tener su propia
región del canto (`camara.region_canto`) y cada par guardado anota qué cámara lo tomó. La resolución de
la webcam se fija en `camara.webcam` (1280 × 720 para la Redragon GW600). Ver la
[guía de armado](docs/armado/README.md#cámara-alternativa-webcam-usb) sobre qué esperar de una webcam de 720p.

### Comandos

| Comando | Uso |
|---|---|
| `python -m archilector ejecutar` | Robot completo + panel web (opciones `--puerto`, `--camara auto\|pi\|webcam`, `--sin-voz`) |
| `python -m archilector ejecutar --puerto ninguno` | Modo asistido sin ESP32 (plan B, Diseño 2.4.8) |
| `python -m archilector prueba-enlace` | Pruebas T2.1 y TC.1–TC.6 (TC.4 es manual) |
| `python -m archilector consola` | Comandos a mano: `TURN`, `RETRY 40`, `CFG PASOS_ARCO 1100`, `LCD 1 HOLA` |
| `python -m archilector pdf 3` | Regenera el PDF del documento 3 |

### Ciclo de operación

```
foto (LED 100 %) → corrección y OCR → clasificador → guardar / leer en voz alta
   → TURN → DONE → foto → verificador ─┬─ página nueva  → siguiente página
                                       ├─ misma página  → RETRY,40 (máx. 2), después pide ayuda
                                       ├─ dos hojas     → BACK y verifica de nuevo; si persiste, pide ayuda
                                       ├─ hoja doblada  → «REVISE LA HOJA» (panel y voz), 3 pitidos, espera #
                                       └─ confianza < 0,80 → otra foto; si se repite, hoja doblada
```

**Las dos páginas con una sola cámara:** con `vision.lado_pagina: ambas` (defecto) se busca el lomo en la
foto (la franja más oscura del centro), se corta ahí y se leen la página izquierda y luego la derecha; con Claude,
las dos a la vez. Cada volteo da dos páginas. `derecha` (con `paso_numeracion: 2`) vuelve a una cara por volteo.

Además del verificador, el número de página impreso que lee el OCR sirve de segunda comprobación:
si salta de más (o no avanza) se corrige la clase, aunque las páginas intermedias no tengan número.

**Teclado (tabla 7.2):** sin documento en curso, **A** empieza a leer y **#** empieza a archivar.
Durante el documento: A seguir · B pausa · C repetir · D parada inmediata · \* página anterior ·
\# confirmar · 1/2 volumen · 4/6 velocidad de la voz · 0 ayuda. En pausa, **#** termina el documento.
**Voz:** «siguiente», «repite», «pausa», «continuar», «anterior», «parar», «ayuda». Solo durante un documento:
la voz no empieza uno (una palabra oída por error movería el mecanismo); para empezar, la tecla A o # o el panel.
El micrófono se elige en `voz.microfono` por fragmentos del nombre en orden de preferencia, p. ej.
`[REDRAGON, USB]` para usar el de la webcam (la GW600 se llama «REDRAGON Live Camera» en `arecord -l`) y, si no está, el micrófono USB; si se desconecta, la escucha
pasa al siguiente sola (ver la [guía de armado](docs/armado/README.md#cámara-alternativa-webcam-usb)).

### Motor de OCR (`ocr.motor` en `config.yaml`)

| Motor | Qué hace | Medido en una Pi 4 con una página real |
|---|---|---|
| `rapidocr` (defecto) | PP-OCRv4 «ch» con onnxruntime | 22–31 s por página, **sin acentos ni ñ**, leyó todas las líneas aun con la cámara inclinada |
| `tesseract` | Tesseract con idioma `spa` | 3–6 s por página, **con acentos, ñ y ¿ ¡** |
| `hibrido` | Tesseract y, si duda, RapidOCR | como Tesseract salvo páginas dudosas |

Tesseract necesita `sudo apt install tesseract-ocr tesseract-ocr-spa` (el instalador ya lo hace); si falta, el
software avisa y usa RapidOCR. Tesseract entrega la caja de cada línea, así que el PDF buscable también funciona
(comprobado: se buscan palabras con acentos en el PDF generado).

Si la imagen no tiene una página legible, Tesseract devuelve un resultado vacío en vez de inventar palabras
(regla `valorar` en `vision/ocr_tesseract.py`: ≥ 12 palabras con confianza ≥ 60, o pocas pero muy seguras).

**Límites conocidos** (pocas fotos de un solo libro, con la cámara en mano y no en el atril):
* Algunas líneas salen con basura en el borde derecho cuando hay sombra o inclinación; la confianza media de la
  página sigue siendo alta, así que el respaldo del híbrido no siempre se activa.
* No leyó el número de página impreso, así que la segunda comprobación del verificador queda sin dato (`None`).
* Con esas mismas fotos, el preproceso del proyecto empeoró el resultado; hay que repetir la prueba con el montaje
  real (cámara fija, luz de la tira LED) antes de cambiar el motor por defecto.

### Panel web

* **Estado:** estado en vivo, último veredicto, controles, nuevo documento y tasa de volteos correctos /
  hojas dobles (criterio para mantener B: ≥ 90 % y ≤ 5 %, Diseño 2.4.5).
* **Documentos:** búsqueda en el texto (SQLite FTS5), páginas, volteos y descarga del PDF buscable.
* **Etiquetar:** cada volteo guarda su par antes/después; se etiqueta con un clic (o teclas 1–4) y se exporta un ZIP para Colab.
* **Calibración:** parámetros de la tabla 5.5 por `CFG`, botones HOME, TURN, RETRY, BACK, STOP, LIGHT, BEEP,
  y elección de la cámara con foto de prueba.

### Panel de pruebas de lectura (herramienta de laboratorio)

`pi/herramientas/panel_pruebas.py` muestra la cámara en vivo y, con un botón, captura la página, la lee con
Tesseract o RapidOCR, la sintetiza con Piper y re-transcribe el audio con Vosk para medir el error de palabras.
Sirve para elegir el motor de OCR, la velocidad de voz y el encuadre antes de montar el robot completo.

```bash
sudo apt install tesseract-ocr tesseract-ocr-spa     # solo si se quiere probar Tesseract
cd pi && .venv/bin/python herramientas/panel_pruebas.py --puerto 8889   # http://<pi>.local:8889
```

Ocupa la cámara (no usar a la vez que `ejecutar` con cámara real), **no tiene autenticación** y solo debe usarse
en la red local. El audio suena en el navegador de quien abre la página, no en la Pi.

---

## 3. Entrenamiento de la IA (Google Colab)

1. Calibre el mecanismo y haga volteos con 3 libros: cada par queda registrado solo.
   Provoque errores (hojas pegadas, soltado tardío, sin presión) hasta tener ~300 pares por clase.
2. Etiquete en **Panel → Etiquetar** y descargue `verificador.zip`.
3. Abra `entrenamiento/verificador_colab.ipynb` en Colab con GPU y ejecútelo: divide 70/15/15 por libro,
   entrena MobileNetV2 en dos etapas, muestra la matriz de confusión, F1 por clase, la comparación con la
   regla de píxeles y verifica las metas (F1 ≥ 0,90; recall de dos hojas ≥ 0,95).
4. Copie `verificador.tflite` y `verificador.txt` a `pi/modelos/` y reinicie el servicio.

Igual para el clasificador de páginas con `clasificador_colab.ipynb` (meta F1 ≥ 0,85).
Mientras no haya modelos, la Pi usa la regla de píxeles y la regla de palabras clave, y lo indica en el panel.

---

## 4. Calibración del mecanismo (Técnico 5.4 y 5.5)

1. T3.0: gire cada rodillo con el engranaje sujeto. Si tiene embrague: `CFG EMBRAGUE 1`.
2. T3.1: ajuste `ANG_REPOSO`, `ANG_CONTACTO`, `ANG_DEPOSITO`, `BARRA_ABIERTA`, `BARRA_CERRADA`.
3. Mida el diámetro D de la goma y fije `PASOS_ARCO = avance × 4096 / (π × D)`; ajústelo en T3.2.
   Si el rodillo gira hacia el lado equivocado: `CFG DIR_RODILLO -1`.
4. T3.3: 20 ciclos `TURN` sin libro; ajuste `VEL_GIRO` si el servo zumba o se traba.
5. Ajuste `vision.region_canto` en `config.yaml` para que el recorte del canto caiga sobre las hojas pasadas.
6. T3.4 / TI.4: 100 volteos; el panel muestra la tasa de correctos y de hojas dobles.

## Pendiente de validar con hardware

Compilado y probado en un PC con el simulador: el firmware compila sin advertencias en sus dos entornos y
las 86 pruebas de la Pi pasan (protocolo, TC.1–TC.6, cada clase del verificador, modos, lectura adelantada,
lectura con Claude, las dos páginas de una foto, voz, PDF y panel web).

Probado en la Raspberry Pi 4, con el ESP32 simulado: captura con Picamera2 (CSI) y con la webcam GW600,
lectura con Claude y respaldo del OCR local, Piper (0,54 veces el tiempo real a 1,8 GHz; con la fuente
débil la CPU baja a 600 MHz y pasa a 1,3), y comandos de voz con Vosk y el micrófono de la webcam.

Falta lo que solo se puede medir en el robot: tiempos reales, ángulos, `PASOS_ARCO`, el sentido del rodillo,
`BACK`, la prueba T6.4 con los servos en marcha y el entrenamiento con datos propios.
