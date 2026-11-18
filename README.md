# 🚀 Xbox Code Sniper (100% Dinámico con OCR Nativo de Windows)

Script todo-en-uno ([sniper_canje.py](sniper_canje.py)) para detectar en tiempo real cualquier código de **Xbox de 25 caracteres** (`XXXXX-XXXXX-XXXXX-XXXXX-XXXXX`) que aparezca en pantalla (YouTube, Twitch, Kick, TikTok, WhatsApp, Discord) y canjearlo automáticamente en milisegundos sin necesidad de calibrar coordenadas manualmente.

---

## ⚡ Características Principales

1. **Soporte Multi-Monitor en caliente (1, 2 o 3 pantallas):**
   - Vigila simultáneamente hasta **3 monitores conectados** con *Frame-Diffing* independiente por pantalla (solo escanea el monitor donde cambian los píxeles).
   - Puedes tener el directo/video o WhatsApp en el **Monitor 1** y la página de canje de Xbox en el **Monitor 2 o 3** (incluso con coordenadas negativas o monitores en distinta posición).
   - Hace clic en píxeles reales del escritorio virtual (`SetCursorPos` + `SendInput` sin coordenadas absolutas), así funciona en 16:9, 21:9 y 32:9, con coordenadas negativas, y activa la ventana automáticamente (`WindowFromPoint` + `SetForegroundWindow`).
2. **Adaptable a todas las resoluciones (720p, 768p, 900p, 1080p, 1200p, 1440p/2K y 4K UHD):**
   - Escala automáticamente pantallas 4K/Ultrawide para mantener alta velocidad de escaneo y aplica **Auto-Zoom 2x/3x BICUBIC** en líneas pequeñas (720p/768p o burbujas de chat).
   - Soporta escalas de zoom de Windows (100%, 125%, 150%, 175%, 200%) con *Per-Monitor DPI Awareness v2*.
3. **Cero calibración:** Localiza automáticamente en vivo dónde está la casilla *"Escribe el código de 25 caracteres"* (tolerando errores típicos del OCR como "eI" en lugar de "el") y los botones **SIGUIENTE** y **CONFIRMAR** de Xbox.
4. **Inyección instantánea Win32 (`SetClipboardData` + `SendInput`):** Copia al portapapeles de kernel en `< 0.5 ms`, enfoca la ventana de Xbox, pega los 25 caracteres de golpe (`Ctrl+V`) y confirma.

---

## 🤖 Instrucciones Rápidas para cualquier Agente de IA

Si eres un **Agente de IA** ayudando al usuario a clonar y ejecutar este repositorio, sigue exactamente estos pasos:

1. **Verificar Python:** Comprueba que Python 3.10+ esté instalado en Windows (`python --version`).
2. **Instalar dependencias:** Ejecuta:
   ```powershell
   python -m pip install mss pillow pyperclip winocr
   ```
3. **Regla crítica de ejecución en Windows (UIPI / `SendInput`):**
   - **NO ejecutes** `sniper_canje.py` dentro de la terminal interna/sandbox del agente de IA, ya que el aislamiento de procesos de Windows bloquea `SendInput` (`eventos=0`) e impide mover el mouse físico sobre el navegador.
   - El script debe ejecutarse directamente en una consola **CMD o PowerShell abierta por el usuario en su escritorio** (o mediante doble clic).
4. **Comando para iniciar:**
   ```powershell
   python sniper_canje.py
   ```

---

## 👤 Instrucciones de Uso (Paso a Paso)

1. Abre `https://www.xbox.com/es-CO/redeem` (o `https://redeem.microsoft.com`), inicia sesión y deja visible la ventana donde dice **"Escribe el código de 25 caracteres"**.
2. Pon tu pantalla dividida (mitad y mitad):
   - **Mitad izquierda:** El directo, video o chat de WhatsApp donde saldrá el código.
   - **Mitad derecha:** La ventana de canje de Xbox.
3. Abre **PowerShell** o **CMD** en esta carpeta y ejecuta:
   ```powershell
   python sniper_canje.py
   ```
4. ¡Listo! Apenas aparezca un código de 25 caracteres en pantalla, el bot moverá el mouse solo a la casilla de Xbox, escribirá el código y pulsará **SIGUIENTE** y **CONFIRMAR** automáticamente.

---

## 🩺 Diagnóstico

Con `DEBUG = True` (al inicio de `sniper_canje.py`) la consola muestra:

- Al arrancar: DPI, si se ejecuta como administrador, resolución de cada monitor y una prueba de OCR por monitor indicando si encontró la casilla y los botones.
- Cada 3 segundos: tiempo promedio del OCR, líneas leídas por monitor y dónde está la casilla.
- En cada clic: dónde se pidió, dónde quedó el cursor y si Windows bloqueó `SendInput`.
- Capturas y texto leído por el OCR en la carpeta `debug/` (al arrancar, cuando se ve un código sin casilla y cuando no aparece CONFIRMAR).

Los códigos se muestran enmascarados (`K9P4X-•••••-•••••-•••••-J8N26`) para que el OCR no los vuelva a leer desde la propia consola. Pon `DEBUG = False` para una salida mínima.
