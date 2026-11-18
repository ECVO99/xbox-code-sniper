import asyncio
import atexit
import ctypes
from ctypes import wintypes
import re
import subprocess
import sys
import time
import traceback
import winsound
from pathlib import Path

# ==============================================================================
# 0. DIAGNÓSTICO EN CONSOLA
# ==============================================================================
DEBUG = True                 # Pon False para desactivar los mensajes de diagnóstico
INTERVALO_ESTADO_SEG = 3.0   # Cada cuánto imprimir el estado del OCR

# Qué pantalla vigilar:
#   "todos"     -> todos los monitores a la vez (recomendado con 2+ pantallas)
#   "principal" -> solo el monitor principal de Windows
#   1, 2, ...   -> un monitor concreto según la lista "Monitor mss[i]" que sale al arrancar
MONITOR_A_VIGILAR = "todos"
CARPETA_DEBUG = Path(__file__).resolve().parent / "debug"
_T0 = time.perf_counter()


def enmascarar(codigo_fmt: str) -> str:
    """Nunca imprimir un código completo: el OCR lo leería de la propia consola y lo canjearía mal."""
    g = re.split(r"[-\s]", codigo_fmt)
    if len(g) == 5:
        return f"{g[0]}-•••••-•••••-•••••-{g[4]}"
    return codigo_fmt[:5] + "…"


RE_LINEA_DE_LOG = re.compile(r"(\[\s*(INFO|WARN|DBG|ERROR)\s*\]|^\W*\d+[.,]\S*s\s*\]|CANJEADO|detectado en)", re.IGNORECASE)


def log(nivel: str, msg: str):
    if nivel == "DBG" and not DEBUG:
        return
    print(f"[{time.perf_counter() - _T0:8.2f}s] [{nivel}] {msg}")

# Auto-instalador automático en el primer arranque (solo necesitas este único archivo)
try:
    import mss
    import mss.tools
    import mss.windows.gdi as mss_gdi
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
except ImportError:
    print("[⚙] Instalando librerías necesarias por única vez...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "mss", "winocr"])
    import mss
    import mss.tools
    import mss.windows.gdi as mss_gdi
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

# Evita bloqueo "BitBlt: Acceso denegado" cuando la ventana de Xbox está abierta
mss_gdi.CAPTUREBLT = 0

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

# Precisión DPI para que las coordenadas coincidan en cualquier pantalla (100%, 125%, 150%)
DPI_MODO = "ninguno"
try:
    _r = ctypes.windll.shcore.SetProcessDpiAwareness(2)
    DPI_MODO = f"PerMonitor (SetProcessDpiAwareness=2, retorno={_r})"
except Exception as _e:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        DPI_MODO = "SystemAware (SetProcessDPIAware)"
    except Exception as _e2:
        DPI_MODO = f"FALLÓ ({_e} / {_e2})"

# ==============================================================================
# 1. CLIC Y TECLEO DIRECTO POR HARDWARE WIN32 (Sin usar portapapeles, < 1 ms)
# ==============================================================================
user32 = ctypes.windll.user32

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002

VK_BACK = 0x08
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_A = 0x41

ULONG_PTR = wintypes.WPARAM


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


def _key_event(vk: int, up: bool = False) -> INPUT:
    scan = user32.MapVirtualKeyW(vk, 0)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.union.ki = KEYBDINPUT(vk, scan, KEYEVENTF_KEYUP if up else 0, 0, 0)
    return inp


def _enviar_inputs(*eventos: INPUT) -> int:
    arr = (INPUT * len(eventos))(*eventos)
    return user32.SendInput(len(eventos), ctypes.byref(arr), ctypes.sizeof(INPUT))


def clic_nativo(x: int, y: int) -> int:
    # Funciona con cualquier resolución/relación de aspecto (16:9, 21:9, 32:9, multi-monitor):
    # se posiciona el cursor en píxeles reales y el clic se envía SIN coordenadas absolutas,
    # evitando la conversión a la escala 0-65535 que dependía del tamaño de pantalla.
    ok_pos = user32.SetCursorPos(int(x), int(y))
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))

    down = INPUT(type=INPUT_MOUSE)
    down.union.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0)
    up = INPUT(type=INPUT_MOUSE)
    up.union.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0)

    n = _enviar_inputs(down, up)
    desfase = abs(pt.x - int(x)) + abs(pt.y - int(y))
    log("DBG", f"Clic pedido en ({x},{y}) -> cursor quedó en ({pt.x},{pt.y}) | SetCursorPos={ok_pos} | eventos={n}/2")
    if not ok_pos or desfase > 2:
        log("WARN", "El cursor NO quedó donde se pidió. Revisa escalado de Windows / monitor principal.")
    if n == 0:
        log("ERROR", f"SendInput bloqueado (GetLastError={ctypes.GetLastError()}). "
                     "Ejecuta desde CMD/PowerShell normal y NO como admin si el navegador no es admin (o ambos como admin).")
    return n


def escribir_codigo_directo(coord, codigo_25: str) -> int:
    if not coord:
        return 0

    clic_nativo(coord[0], coord[1])
    time.sleep(0.020)
    clic_nativo(coord[0], coord[1])
    time.sleep(0.020)

    # Limpiar texto anterior (Ctrl+A + Backspace)
    _enviar_inputs(
        _key_event(VK_CONTROL, False),
        _key_event(VK_A, False),
        _key_event(VK_A, True),
        _key_event(VK_CONTROL, True),
        _key_event(VK_BACK, False),
        _key_event(VK_BACK, True),
    )
    time.sleep(0.008)

    # Escribir los 25 caracteres directamente por hardware
    enviados = 0
    for ch in codigo_25.upper():
        if ch.isalnum():
            vk = ord(ch)
            enviados += _enviar_inputs(_key_event(vk, False), _key_event(vk, True))
            time.sleep(0.002)

    time.sleep(0.015)
    enviados += _enviar_inputs(_key_event(VK_RETURN, False), _key_event(VK_RETURN, True))
    esperados = 25 * 2 + 2
    nivel = "DBG" if enviados >= esperados else "WARN"
    log(nivel, f"Teclas enviadas: {enviados}/{esperados}")
    return enviados


def presionar_enter_nativo():
    return _enviar_inputs(_key_event(VK_RETURN, False), _key_event(VK_RETURN, True))


# ==============================================================================
# 2. MOTOR OCR NATIVO DE WINDOWS CON COORDENADAS EN TIEMPO REAL
# ==============================================================================
class FastWindowsOCR:
    def __init__(self, lang_tag: str = "es"):
        lang = Language(lang_tag)
        if OcrEngine.is_language_supported(lang):
            self.engine = OcrEngine.try_create_from_language(lang)
        else:
            self.engine = OcrEngine.try_create_from_user_profile_languages()
        self.loop = asyncio.new_event_loop()
        atexit.register(self.close)

    def close(self):
        if getattr(self, "loop", None) and not self.loop.is_closed():
            self.loop.close()
        self.engine = None

    async def _recognize_async(self, bgra_bytes: bytes, width: int, height: int):
        writer = DataWriter()
        writer.write_bytes(bgra_bytes)
        sb = SoftwareBitmap.create_copy_from_buffer(
            writer.detach_buffer(), BitmapPixelFormat.BGRA8, width, height
        )
        return await self.engine.recognize_async(sb)

    def recognize_with_boxes(self, bgra_bytes: bytes, width: int, height: int, offset_x: int = 0, offset_y: int = 0):
        res = self.loop.run_until_complete(self._recognize_async(bgra_bytes, width, height))
        if not res:
            return []
        lineas = []
        for line in res.lines:
            words = line.words
            if not words:
                continue
            x0 = min(w.bounding_rect.x for w in words)
            y0 = min(w.bounding_rect.y for w in words)
            x1 = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
            y1 = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
            lineas.append({
                "text": line.text.strip(),
                "cx": int(offset_x + (x0 + x1) / 2),
                "cy": int(offset_y + (y0 + y1) / 2),
                "x": int(offset_x + x0),
                "y": int(offset_y + y0),
                "w": int(x1 - x0),
                "h": int(y1 - y0),
            })
        return lineas


# ==============================================================================
# 3. INTELIGENCIA DE CÓDIGOS XBOX + DETECCIÓN DINÁMICA DE CASILLA Y BOTONES
# ==============================================================================
XBOX_VALID_CHARS = set("BCDFGHJKMPQRTVWXYZ2346789")
TABLA_CORRECCION_XBOX = str.maketrans({
    "0": "Q", "O": "D", "1": "7", "I": "J", "L": "T",
    "5": "6", "S": "9", "A": "4", "E": "F", "U": "V", "N": "M",
})

PATRON_XBOX_5X5 = re.compile(
    r"(?<![A-Z0-9])([A-Z0-9]{5})[\s\-–—._•:;|/\\]+([A-Z0-9]{5})[\s\-–—._•:;|/\\]+([A-Z0-9]{5})[\s\-–—._•:;|/\\]+([A-Z0-9]{5})[\s\-–—._•:;|/\\]+([A-Z0-9]{5})(?![A-Z0-9])",
    re.IGNORECASE,
)

# Tolera errores típicos del OCR: "eI", "e1", "e|", "códiqo", "2S"... Debe empezar la línea con "Escribe"
# para no confundirse con la descripción "...escribe tu código de 25 caracteres en el espacio siguiente".
RE_PLACEHOLDER_INPUT = re.compile(
    r"^\W*escrib\S*\s+e[lI1|!]\s+c\S*di\S*\s+de\s+2[5S]|enter\s+.*25-character", re.IGNORECASE
)
RE_COMO_ESTE_BAJO_INPUT = re.compile(r"^(como\s+este|like\s+this)\s*:", re.IGNORECASE)
RE_ETIQUETA_ARRIBA_INPUT = re.compile(r"^se\s+agregar\S*\s+a\s+\S+@", re.IGNORECASE)
RE_BOTON_SIGUIENTE = re.compile(r"^(siguiente|sibuien\S*|siguien\S*|next)$", re.IGNORECASE)
RE_BOTON_CANCELAR = re.compile(r"^(cancelar|cancel)$", re.IGNORECASE)
RE_BOTON_CONFIRMAR = re.compile(r"^(confirmar|confirm|canjear\s+ahora|redeem\s+now)$", re.IGNORECASE)


def _validar_codigo_25(raw25: str, tiene_guiones: bool):
    raw25 = raw25.upper()
    if len(raw25) != 25 or not raw25.isalnum() or raw25 == "X" * 25:
        return None

    digit_count = sum(1 for c in raw25 if c.isdigit())
    valid_xbox_count = sum(1 for c in raw25 if c in XBOX_VALID_CHARS)

    if tiene_guiones and digit_count >= 3:
        return raw25, "-".join(raw25[i : i + 5] for i in range(0, 25, 5))

    if valid_xbox_count >= 21 and digit_count >= 2:
        corregido = raw25.translate(TABLA_CORRECCION_XBOX)
        return corregido, "-".join(corregido[i : i + 5] for i in range(0, 25, 5))

    return None


def detectar_codigos_en_lineas(lineas, casilla_xbox_coord=None):
    encontrados = []
    vistos = set()

    for item in lineas:
        txt = item["text"].upper()
        cx, cy = item["cx"], item["cy"]

        if any(m in txt for m in ("HTTP", "WWW.", ".COM", "UTM_", "=", "&", "?", "COPY-LINK")):
            continue
        if RE_LINEA_DE_LOG.search(item["text"]):
            continue  # Es la consola de este mismo script

        if casilla_xbox_coord and abs(cx - casilla_xbox_coord[0]) < 240 and abs(cy - casilla_xbox_coord[1]) < 42:
            continue

        for m in PATRON_XBOX_5X5.finditer(txt):
            raw25 = "".join(m.groups())
            tiene_guion = any(c in txt[m.start() : m.end()] for c in "-–—._")
            res = _validar_codigo_25(raw25, tiene_guiones=tiene_guion)
            if res and res[0] not in vistos:
                vistos.add(res[0])
                encontrados.append((res[0], res[1], (cx, cy)))

        palabras = [(m.group(0), m.start(), m.end()) for m in re.finditer(r"[A-Z0-9]+", txt)]
        n = len(palabras)
        for i in range(n):
            if len(palabras[i][0]) % 5 != 0:
                continue
            acum = ""
            tiene_guion = False
            for j in range(i, min(i + 5, n)):
                if len(palabras[j][0]) % 5 != 0:
                    break
                if j > i:
                    sep = txt[palabras[j - 1][2] : palabras[j][1]]
                    if len(sep) > 4 or any(c.isalnum() for c in sep):
                        break
                    if any(c in sep for c in "-–—._"):
                        tiene_guion = True
                acum += palabras[j][0]
                if len(acum) == 25:
                    res = _validar_codigo_25(acum, tiene_guiones=tiene_guion)
                    if res and res[0] not in vistos:
                        vistos.add(res[0])
                        encontrados.append((res[0], res[1], (cx, cy)))
                    break
                elif len(acum) > 25:
                    break

    return encontrados


def localizar_ui_xbox_dinamica(lineas):
    coord_input = None
    coord_siguiente = None
    coord_confirmar = None
    linea_cancelar = None

    for item in lineas:
        if RE_PLACEHOLDER_INPUT.search(item["text"]):
            coord_input = (item["cx"], item["cy"])
            break

    if not coord_input:
        for item in lineas:
            if RE_COMO_ESTE_BAJO_INPUT.search(item["text"]):
                coord_input = (item["x"] + 150, item["y"] - 48)
                break
            if RE_ETIQUETA_ARRIBA_INPUT.search(item["text"]):
                coord_input = (item["x"] + 150, item["y"] + item["h"] + 42)

    for item in lineas:
        limpio = re.sub(r"[^A-Za-zÁÉÍÓÚáéíóúñÑ\s]", "", item["text"]).strip()
        if not limpio or item["w"] > 400 or item["h"] > 100:
            continue
        if RE_BOTON_CONFIRMAR.match(limpio):
            coord_confirmar = (item["cx"], item["cy"])
        elif RE_BOTON_SIGUIENTE.match(limpio):
            coord_siguiente = (item["cx"], item["cy"])
        elif RE_BOTON_CANCELAR.match(limpio):
            linea_cancelar = item

    if not coord_siguiente and linea_cancelar:
        coord_siguiente = (linea_cancelar["cx"] + int(linea_cancelar["w"] * 3.8), linea_cancelar["cy"])

    return coord_input, coord_siguiente, coord_confirmar


# ==============================================================================
# 4. BUCLE PRINCIPAL (UN SOLO ARCHIVO, 100% AUTOMÁTICO)
# ==============================================================================
def _guardar_diagnostico(sct, s, lineas, etiqueta: str):
    try:
        CARPETA_DEBUG.mkdir(exist_ok=True)
        png = CARPETA_DEBUG / f"{etiqueta}.png"
        txt = CARPETA_DEBUG / f"{etiqueta}_ocr.txt"
        mss.tools.to_png(s.rgb, s.size, output=str(png))
        with open(txt, "w", encoding="utf-8") as f:
            for it in lineas:
                f.write(f"({it['x']},{it['y']}) w={it['w']} h={it['h']} centro=({it['cx']},{it['cy']}) | {it['text']}\n")
        log("INFO", f"Diagnóstico guardado: {png.name} y {txt.name} en {CARPETA_DEBUG}")
    except Exception as e:
        log("WARN", f"No se pudo guardar diagnóstico: {e}")


def main():
    print("=" * 72)
    print("  🚀 SNIPER XBOX 100% DINÁMICO (ARCHIVO ÚNICO - CERO CALIBRACIÓN)")
    print("=" * 72)
    print("  1. Pon en pantalla dividida el directo/WhatsApp y xbox.com/redeem.")
    print("  2. El bot localiza solo la casilla de Xbox y escribe el código al instante.")
    print("=" * 72)

    # ---------- Validaciones de arranque ----------
    log("INFO", f"Python {sys.version.split()[0]} | DPI: {DPI_MODO}")
    try:
        es_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        es_admin = None
    log("INFO", f"Ejecutando como administrador: {es_admin}")
    log("INFO", f"GetSystemMetrics pantalla principal: {user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)} | "
                f"escritorio virtual: {user32.GetSystemMetrics(78)}x{user32.GetSystemMetrics(79)}")

    ocr = FastWindowsOCR(lang_tag="es")
    if ocr.engine is None:
        log("ERROR", "No se pudo crear el motor OCR de Windows. Instala el paquete de idioma Español "
                     "(Configuración > Hora e idioma > Idioma) con 'Reconocimiento óptico de caracteres'.")
        return
    try:
        log("INFO", f"Idioma OCR: {ocr.engine.recognizer_language.language_tag} | "
                    f"Tamaño máx. imagen OCR: {OcrEngine.max_image_dimension}px")
    except Exception:
        pass
    ocr.recognize_with_boxes(bytes(64 * 64 * 4), 64, 64)

    sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
    for i, m in enumerate(sct.monitors):
        log("INFO", f"Monitor mss[{i}]{' (todos)' if i == 0 else ''}: {m['width']}x{m['height']} en ({m['left']},{m['top']})")
    if MONITOR_A_VIGILAR == "todos":
        idx = 0
    elif MONITOR_A_VIGILAR == "principal":
        idx = next((i for i, m in enumerate(sct.monitors[1:], 1) if m["left"] == 0 and m["top"] == 0), 1)
    else:
        idx = int(MONITOR_A_VIGILAR)
        if not 0 <= idx < len(sct.monitors):
            log("ERROR", f"MONITOR_A_VIGILAR={idx} no existe. Usando todos.")
            idx = 0
    monitor = sct.monitors[idx]
    principal = next((i for i, m in enumerate(sct.monitors[1:], 1) if m["left"] == 0 and m["top"] == 0), None)
    log("INFO", f"Monitor principal de Windows: mss[{principal}]")
    log("INFO", f"Monitor capturado: mss[{idx}]{' (todos)' if idx == 0 else ''} {monitor['width']}x{monitor['height']} "
                f"desde ({monitor['left']},{monitor['top']}) (proporción {monitor['width'] / monitor['height']:.2f}:1)")
    try:
        max_dim = OcrEngine.max_image_dimension
        if max(monitor["width"], monitor["height"]) > max_dim:
            log("ERROR", f"La captura ({monitor['width']}px) supera el máximo del OCR ({max_dim}px).")
    except Exception:
        pass

    # Prueba inicial: captura + OCR + guardado
    t = time.perf_counter()
    s0 = sct.grab(monitor)
    lin0 = ocr.recognize_with_boxes(s0.raw, s0.width, s0.height, monitor["left"], monitor["top"])
    log("INFO", f"Prueba OCR: {len(lin0)} líneas leídas en {(time.perf_counter() - t) * 1000:.0f} ms")
    in0, sig0, conf0 = localizar_ui_xbox_dinamica(lin0)
    log("INFO" if in0 else "WARN", f"Casilla Xbox: {in0 or 'NO ENCONTRADA'} | Siguiente: {sig0} | Confirmar: {conf0}")
    if not in0:
        pistas = [it["text"] for it in lin0 if re.search(r"c.digo|25|caracteres|character|canjear|redeem", it["text"], re.I)]
        log("WARN", f"Textos parecidos a la página de Xbox vistos por el OCR: {pistas[:8] or 'ninguno'}")
    _guardar_diagnostico(sct, s0, lin0, "arranque")

    codigos_procesados = set()
    descartes_vistos = set()
    ultima_coord_xbox = None
    ultimo_btn_siguiente = None
    ultimo_raw = None
    t_estado = time.perf_counter()
    frames = 0
    ms_ocr_total = 0.0
    n_lineas = 0
    errores = 0

    print()
    log("INFO", "✔ Vigilando pantalla en vivo... (Ctrl+C para detener)\n")

    while True:
        try:
            s = sct.grab(monitor)
            if s.raw == ultimo_raw:
                time.sleep(0.005)
                continue
            ultimo_raw = s.raw
            t_frame = time.perf_counter()

            lineas = ocr.recognize_with_boxes(s.raw, s.width, s.height, monitor["left"], monitor["top"])
            frames += 1
            ms_ocr_total += (time.perf_counter() - t_frame) * 1000.0
            n_lineas = len(lineas)
            dyn_input, dyn_sig, _ = localizar_ui_xbox_dinamica(lineas)

            if dyn_input:
                if not ultima_coord_xbox or (
                    abs(dyn_input[0] - ultima_coord_xbox[0]) > 12 or abs(dyn_input[1] - ultima_coord_xbox[1]) > 12
                ):
                    log("INFO", f"🎯 CASILLA XBOX LOCALIZADA en {dyn_input} | Botón Siguiente: {dyn_sig}")
                ultima_coord_xbox = dyn_input
            if dyn_sig:
                ultimo_btn_siguiente = dyn_sig

            # Estado periódico
            if time.perf_counter() - t_estado >= INTERVALO_ESTADO_SEG:
                prom = ms_ocr_total / frames if frames else 0
                log("DBG", f"Estado: {frames} frames, OCR prom {prom:.0f} ms, {n_lineas} líneas | "
                           f"casilla en este frame: {dyn_input or 'NO'} | última casilla: {ultima_coord_xbox} | "
                           f"siguiente: {ultimo_btn_siguiente} | errores: {errores}")
                t_estado = time.perf_counter()
                frames, ms_ocr_total = 0, 0.0

            codigos = detectar_codigos_en_lineas(lineas, casilla_xbox_coord=ultima_coord_xbox)
            validos = {c[0] for c in codigos}

            # Candidatos que parecen código pero no pasaron el filtro
            for it in lineas:
                compacto = re.sub(r"[^A-Za-z0-9]", "", it["text"])
                mayus = sum(1 for c in compacto if c.isupper() or c.isdigit())
                if RE_LINEA_DE_LOG.search(it["text"]):
                    continue
                if len(compacto) >= 20 and mayus / len(compacto) >= 0.8 and it["text"] not in descartes_vistos and not any(v in compacto.upper().translate(TABLA_CORRECCION_XBOX) or v in compacto.upper() for v in validos):
                    descartes_vistos.add(it["text"])
                    log("DBG", f"Texto tipo código rechazado en ({it['cx']},{it['cy']}): empieza '{compacto[:5]}…' ({len(compacto)} alfanum.)")

            for c25, cfmt, pos_codigo in reversed(codigos):
                if c25 in codigos_procesados:
                    continue
                if not ultima_coord_xbox:
                    if c25 not in descartes_vistos:
                        descartes_vistos.add(c25)
                        log("WARN", f"Código {enmascarar(cfmt)} visto en {pos_codigo}, pero NO se ha localizado la casilla de Xbox. "
                                    "No se escribe. Revisa debug/arranque_ocr.txt.")
                        _guardar_diagnostico(sct, s, lineas, "codigo_sin_casilla")
                    continue

                codigos_procesados.add(c25)
                log("INFO", f"Código {enmascarar(cfmt)} visto en {pos_codigo}. Escribiendo en casilla {ultima_coord_xbox}...")
                n_ev = escribir_codigo_directo(ultima_coord_xbox, c25)
                ms = (time.perf_counter() - t_frame) * 1000.0
                log("INFO", f"⚡ Escrito en {ms:.1f} ms (eventos={n_ev}): {enmascarar(cfmt)}")

                # Vigilar y pulsar SIGUIENTE y CONFIRMAR automáticamente
                confirmado = False
                for intento in range(20):
                    time.sleep(0.14)
                    s2 = sct.grab(monitor)
                    lin2 = ocr.recognize_with_boxes(s2.raw, s2.width, s2.height, monitor["left"], monitor["top"])
                    _, sig2, conf2 = localizar_ui_xbox_dinamica(lin2)
                    log("DBG", f"  Intento {intento + 1}/20: Siguiente={sig2} Confirmar={conf2}")
                    if conf2:
                        clic_nativo(conf2[0], conf2[1])
                        presionar_enter_nativo()
                        log("INFO", f"  ✔ AUTO-CONFIRMAR: botón 'CONFIRMAR' pulsado en {conf2}")
                        confirmado = True
                        break
                    target_sig = sig2 or ultimo_btn_siguiente
                    if target_sig:
                        clic_nativo(target_sig[0], target_sig[1])
                    elif intento == 0:
                        log("WARN", "  No se encontró el botón SIGUIENTE ni CANCELAR en pantalla.")
                if not confirmado:
                    log("WARN", "  No apareció el botón CONFIRMAR tras 20 intentos.")
                    _guardar_diagnostico(sct, s2, lin2, "sin_confirmar")

                try:
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)
                except Exception:
                    pass
                ultimo_raw = None
                break

            time.sleep(0.005)
        except KeyboardInterrupt:
            print()
            log("INFO", "⏹ Detenido.")
            break
        except Exception as e:
            errores += 1
            if errores <= 5 or errores % 50 == 0:
                log("ERROR", f"Excepción #{errores} en el bucle: {e!r}")
                if DEBUG:
                    traceback.print_exc()
            time.sleep(0.02)


if __name__ == "__main__":
    main()
