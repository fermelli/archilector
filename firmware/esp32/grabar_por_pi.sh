#!/bin/bash
# Graba el ESP32 conectado por USB a la Raspberry Pi, cuando se trabaja en remoto.
# Compila aquí con PlatformIO, copia los binarios a la Pi y los graba allá con esptool
# (en la Pi: ~/ArchiLector/pi/.venv/bin/pip install esptool). Detiene ArchiLector mientras graba.
#
#   ./grabar_por_pi.sh                    -> firmware principal
#   ./grabar_por_pi.sh prueba_salidas     -> prueba T2.3
#   ./grabar_por_pi.sh archilector otra-pi   (host de ssh; por defecto pi-conteo)
set -e
cd "$(dirname "$0")"
ENTORNO=${1:-archilector}
PI=${2:-pi-conteo}
PIO=$(command -v pio || echo ~/.platformio/penv/Scripts/pio.exe)

"$PIO" run -e "$ENTORNO"
B=.pio/build/$ENTORNO
APP0=~/.platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin
scp -q "$B/bootloader.bin" "$B/partitions.bin" "$B/firmware.bin" "$APP0" "$PI":/tmp/

ssh "$PI" 'set -e
P=/dev/archilector-esp32
[ -e "$P" ] || P=$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -1)
[ -n "$P" ] || { echo "No hay un ESP32 conectado por USB a la Pi"; exit 1; }
sudo systemctl stop archilector
trap "sudo systemctl start archilector" EXIT   # ArchiLector vuelve aunque falle la grabación
~/ArchiLector/pi/.venv/bin/python -m esptool --chip esp32 --port "$P" --baud 115200 write-flash -z \
  0x1000 /tmp/bootloader.bin 0x8000 /tmp/partitions.bin 0xe000 /tmp/boot_app0.bin 0x10000 /tmp/firmware.bin
echo "ESP32 grabado ($P)"'
