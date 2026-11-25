#!/usr/bin/env bash
# ArchiLector · instalación en Raspberry Pi OS Lite de 64 bits (Bookworm)
# Documento Técnico v2.2, sección 6.4. Ejecutar desde la carpeta pi/:
#     bash deploy/instalar.sh
set -euo pipefail

USUARIO="${USUARIO:-archilector}"
CARPETA="$(cd "$(dirname "$0")/.." && pwd)"
cd "$CARPETA"

echo "==> Paquetes del sistema"
sudo apt-get update
sudo apt-get install -y python3-venv python3-picamera2 python3-libcamera libcamera-apps \
    alsa-utils zram-tools libatlas-base-dev unzip wget tesseract-ocr tesseract-ocr-spa

echo "==> zram para aliviar los 2 GB de RAM"
echo -e "ALGO=zstd\nPERCENT=50" | sudo tee /etc/default/zramswap >/dev/null
sudo systemctl restart zramswap || true

echo "==> Usuario del servicio (${USUARIO}) en los grupos dialout, video y audio"
id "$USUARIO" >/dev/null 2>&1 || sudo useradd -m -s /bin/bash "$USUARIO"
sudo usermod -aG dialout,video,audio "$USUARIO"

echo "==> Entorno de Python (con acceso a picamera2 del sistema)"
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .[pi]

# picamera2 (paquete de apt) usa simplejpeg, compilado contra numpy 1.x. Sin este paso pip
# deja numpy 2.x y OpenCV 5 en el venv y la cámara falla con
# "numpy.dtype size changed, may indicate binary incompatibility".
# Probado en la Pi 4 (Bookworm, Python 3.11): CamaraPi captura 2592x1944 y `pip check` queda limpio.
.venv/bin/pip install "numpy<2" "opencv-python==4.10.0.84" "opencv-python-headless==4.10.0.84" "ml-dtypes<0.5"

echo "==> Modelos de voz (Piper y Vosk) en español"
mkdir -p modelos/voz
if [ ! -f modelos/voz/es_ES-davefx-medium.onnx ]; then
    base="https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/davefx/medium"
    wget -q -O modelos/voz/es_ES-davefx-medium.onnx "$base/es_ES-davefx-medium.onnx"
    wget -q -O modelos/voz/es_ES-davefx-medium.onnx.json "$base/es_ES-davefx-medium.onnx.json"
fi
if [ ! -d modelos/vosk-model-small-es-0.42 ]; then
    wget -q -O /tmp/vosk-es.zip https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip
    unzip -q /tmp/vosk-es.zip -d modelos && rm /tmp/vosk-es.zip
fi

echo "==> Configuración"
[ -f config.yaml ] || cp config.ejemplo.yaml config.yaml

echo "==> Regla udev: /dev/archilector-esp32"
sudo cp deploy/99-archilector.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger

echo "==> Servicio systemd"
sed "s#/home/archilector/ArchiLector/pi#${CARPETA}#g; s#User=archilector#User=${USUARIO}#; s#Group=archilector#Group=${USUARIO}#" \
    deploy/archilector.service | sudo tee /etc/systemd/system/archilector.service >/dev/null
sudo chown -R "$USUARIO" "$CARPETA"
sudo systemctl daemon-reload
sudo systemctl enable archilector

cat <<EOF

Listo. Pasos siguientes:
  1. Verifique la cámara:        rpicam-still -o prueba.jpg          (prueba T4.1)
  2. Verifique el ESP32:         ls -l /dev/archilector-esp32         (prueba T5.2)
  3. Pruebas de comunicación:    .venv/bin/python -m archilector prueba-enlace
  4. Arranque el servicio:       sudo systemctl start archilector
  5. Panel web:                  http://$(hostname).local:8000
  Registro en vivo:              journalctl -u archilector -f
EOF
