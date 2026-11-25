// ArchiLector · funciones comunes del panel: estado en vivo, órdenes y avisos.
const Panel = (() => {
  // estado del orquestador -> [texto, tono, ¿en marcha?]
  const ESTADOS = {
    inactivo: ["En espera", "neutro", false],
    iniciando: ["Iniciando", "info", true],
    leyendo: ["Leyendo en voz alta", "ok", true],
    volteando: ["Pasando la hoja", "info", true],
    esperando_hoja: ["Esperando que pase la hoja", "info", true],
    requiere_usuario: ["Necesita ayuda", "peligro", true],
    pausa: ["En pausa", "aviso", false],
    parada: ["Parada", "peligro", false],
    reanudando: ["Reanudando", "info", true],
    terminado: ["Terminado", "ok", false],
  };
  const CLASES = {
    pagina_nueva: ["Página nueva", "ok"],
    misma_pagina: ["No pasó la hoja", "aviso"],
    dos_hojas: ["Pasaron dos hojas", "peligro"],
    hoja_doblada: ["Hoja doblada", "peligro"],
  };
  const MODOS = { lectura: "Lectura", archivo: "Archivo", asistido: "Asistido" };
  const ORDENES = {
    iniciar: "Iniciar / seguir", pausa: "Pausa", repetir: "Repetir", siguiente: "Siguiente",
    anterior: "Anterior", confirmar: "Confirmar", terminar: "Terminar documento", parar: "Parada", ayuda: "Ayuda",
  };
  const oyentes = [];
  let ultimo = null;

  function estado(clave) { return ESTADOS[clave] || [String(clave || "—").replace(/_/g, " "), "neutro", false]; }
  function clase(clave) { return CLASES[clave] || [String(clave || "—").replace(/_/g, " "), "neutro"]; }

  function tostada(texto, tono = "") {
    const caja = document.getElementById("tostadas");
    if (!caja) return;
    const t = document.createElement("div");
    t.className = "tostada " + tono;
    t.textContent = texto;
    caja.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }

  async function orden(nombre) {
    try {
      const r = await fetch("/control/" + encodeURIComponent(nombre), { method: "POST" });
      if (!r.ok) throw new Error(r.status);
      tostada("Orden enviada: " + (ORDENES[nombre] || nombre));
    } catch (e) {
      tostada("No se pudo enviar la orden", "peligro");
    }
    actualizar();
  }

  async function actualizar() {
    let e;
    try {
      e = await (await fetch("/api/estado", { cache: "no-store" })).json();
    } catch (err) {
      pintarBarra(null);
      return;
    }
    ultimo = e;
    pintarBarra(e);
    oyentes.forEach((f) => { try { f(e); } catch (err) { console.error(err); } });
  }

  function pintarBarra(e) {
    const punto = document.getElementById("barra-punto");
    const texto = document.getElementById("barra-texto");
    if (!punto || !texto) return;
    if (!e) { punto.className = "punto peligro"; texto.textContent = "Sin conexión"; return; }
    const [nombre, tono, vivo] = estado(e.estado);
    punto.className = "punto " + tono + (vivo ? " vivo" : "");
    texto.textContent = nombre;
  }

  function alActualizar(f) { oyentes.push(f); if (ultimo) f(ultimo); }

  actualizar();
  setInterval(actualizar, 1500);
  return { estado, clase, MODOS, tostada, orden, alActualizar, actualizar };
})();
