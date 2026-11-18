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
# 0. AUTO-INSTALADOR DE DEPENDENCIAS (Archivo único 100% autónomo)
# ==============================================================================
try:
    import mss
    import mss.tools
    import mss.windows.gdi as mss_gdi
    from PIL import Image
    import pyperclip
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
except ImportError:
    print("[⚙] Instalando librerías necesarias por única vez (mss, pillow, pyperclip, winocr)...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "mss", "pillow", "pyperclip", "winocr"]
    )
    import mss
    import mss.tools
    import mss.windows.gdi as mss_gdi
    from PIL import Image
    import pyperclip
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

# Desactiva CAPTUREBLT para evitar "BitBlt: Acceso denegado" con la App/Web de Xbox en Windows 11
mss_gdi.CAPTUREBLT = 0

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

# ==============================================================================
# DIAGNÓSTICO EN CONSOLA
# ==============================================================================
DEBUG = True                 # Pon False para desactivar los mensajes de diagnóstico
INTERVALO_ESTADO_SEG = 3.0   # Cada cuánto imprimir el estado del OCR
CARPETA_DEBUG = Path(__file__).resolve().parent / "debug"
_T0 = time.perf_counter()


def enmascarar(codigo_fmt: str) -> str:
    """Nunca imprimir un código completo: el OCR lo leería de la propia consola y lo canjearía mal."""
    g = re.split(r"[-\s]", codigo_fmt)
    if len(g) == 5:
        return f"{g[0]}-•••••-•••••-•••••-{g[4]}"
    return codigo_fmt[:5] + "…"


# Líneas que son la salida de este mismo script (para no leerse a sí mismo en la consola)
RE_LINEA_DE_LOG = re.compile(
    r"(\[\s*(INFO|WARN|DBG|ERROR)\s*\]|^\W*\d+[.,]\S*s\s*\]|CANJEADO|detectado en|visto en)", re.IGNORECASE
)


def log(nivel: str, msg: str):
    if nivel == "DBG" and not DEBUG:
        return
    print(f"[{time.perf_counter() - _T0:8.2f}s] [{nivel}] {msg}")


# Activar Per-Monitor DPI Awareness v2 para precisión exacta en escalas 100%, 125%, 150%, 175%, 200%
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
# 1. MOTOR NATIVO WIN32 MULTI-MONITOR (VIRTUAL DESKTOP + PORTAPAPELES KERNEL)
# ==============================================================================
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

GMEM_MOVEABLE = 0x0002
CF_UNICODETEXT = 13

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE

# Métricas de escritorio virtual multi-monitor de Windows
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CMONITORS = 80
GA_ROOT = 2

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002

VK_BACK = 0x08
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_A = 0x41
VK_V = 0x56

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


def copiar_portapapeles_win32(texto: str):
    """Copia el código al portapapeles de Windows en < 0.5 ms usando memoria global Win32."""
    data = (texto + "\0").encode("utf-16-le")
    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if h_mem:
                    ptr = kernel32.GlobalLock(h_mem)
                    if ptr:
                        ctypes.memmove(ptr, data, len(data))
                        kernel32.GlobalUnlock(h_mem)
                        if user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                            return
            finally:
                user32.CloseClipboard()
        time.sleep(0.0005)
    try:
        pyperclip.copy(texto)
    except Exception:
        pass


def enfocar_ventana_en_punto(x: int, y: int):
    """Trae al frente la ventana ubicada en (x, y) aunque el usuario esté usando otro monitor."""
    try:
        pt = wintypes.POINT(int(x), int(y))
        hwnd = user32.WindowFromPoint(pt)
        if hwnd:
            root_hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
            user32.SetForegroundWindow(root_hwnd)
    except Exception:
        pass


def clic_multimonitor(x: int, y: int) -> int:
    """
    Mueve el cursor y hace clic con precisión exacta en configuraciones de 1, 2 o 3 monitores
    (coordenadas negativas, 16:9, 21:9, 32:9 y resoluciones mixtas 720p / 1080p / 1440p / 4K).
    El cursor se posiciona en píxeles reales con SetCursorPos y el clic se envía SIN coordenadas
    absolutas: así no depende de convertir a la escala 0-65535 ni del tamaño del escritorio virtual.
    """
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


def inyectar_codigo_en_xbox(coord, codigo_25: str) -> int:
    """
    1. Copia los 25 caracteres al portapapeles de Windows en < 0.5 ms.
    2. Activa la ventana destino (aunque esté en otro monitor) y hace doble clic rápido en la casilla.
    3. Limpia cualquier texto residual (Ctrl+A + Backspace), pega de golpe (Ctrl+V) y presiona Enter.
    """
    if not coord:
        return 0

    copiar_portapapeles_win32(codigo_25.upper())
    enfocar_ventana_en_punto(coord[0], coord[1])

    clic_multimonitor(coord[0], coord[1])
    time.sleep(0.018)
    clic_multimonitor(coord[0], coord[1])
    time.sleep(0.018)

    enviados = _enviar_inputs(
        _key_event(VK_CONTROL, False),
        _key_event(VK_A, False),
        _key_event(VK_A, True),
        _key_event(VK_CONTROL, True),
        _key_event(VK_BACK, False),
        _key_event(VK_BACK, True),
        _key_event(VK_CONTROL, False),
        _key_event(VK_V, False),
        _key_event(VK_V, True),
        _key_event(VK_CONTROL, True),
        _key_event(VK_RETURN, False),
        _key_event(VK_RETURN, True),
    )
    nivel = "DBG" if enviados >= 12 else "WARN"
    log(nivel, f"Teclas enviadas (Ctrl+A, Borrar, Ctrl+V, Enter): {enviados}/12")
    return enviados


def presionar_enter_nativo():
    return _enviar_inputs(_key_event(VK_RETURN, False), _key_event(VK_RETURN, True))


# ==============================================================================
# 2. MOTOR OCR NATIVO DE WINDOWS ADAPTATIVO (SOPORTA DESDE 720P HASTA 4K UHD)
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

    def recognize_text(self, bgra_bytes: bytes, width: int, height: int) -> str:
        res = self.loop.run_until_complete(self._recognize_async(bgra_bytes, width, height))
        return res.text.strip() if res and res.text else ""

    def recognize_monitor(self, bgra_bytes: bytes, width: int, height: int, offset_x: int = 0, offset_y: int = 0):
        """
        Escanea un monitor adaptándose automáticamente a su resolución (720p, 1080p, 1440p, Ultrawide, 4K).
        Si la resolución supera 2560px de ancho (ej. 4K 3840x2160), reescala 0.5x para máxima fluidez
        y proyecta las coordenadas de vuelta al tamaño nativo real del monitor.
        """
        escala = 1.0
        ocr_bytes = bgra_bytes
        ocr_w, ocr_h = width, height

        if width > 2600 or height > 1600:
            escala = 0.5
            ocr_w = max(1, width // 2)
            ocr_h = max(1, height // 2)
            img = Image.frombytes("RGBA", (width, height), bgra_bytes)
            img_small = img.resize((ocr_w, ocr_h), Image.Resampling.BILINEAR)
            ocr_bytes = img_small.tobytes()

        res = self.loop.run_until_complete(self._recognize_async(ocr_bytes, ocr_w, ocr_h))
        if not res:
            return []

        inv = 1.0 / escala
        lineas = []
        for line in res.lines:
            words = line.words
            if not words:
                continue
            x0 = min(w.bounding_rect.x for w in words) * inv
            y0 = min(w.bounding_rect.y for w in words) * inv
            x1 = max(w.bounding_rect.x + w.bounding_rect.width for w in words) * inv
            y1 = max(w.bounding_rect.y + w.bounding_rect.height for w in words) * inv

            lineas.append({
                "text": line.text.strip(),
                "cx": int(offset_x + (x0 + x1) / 2),
                "cy": int(offset_y + (y0 + y1) / 2),
                "x": int(offset_x + x0),
                "y": int(offset_y + y0),
                "rx": int(x0),
                "ry": int(y0),
                "w": int(x1 - x0),
                "h": int(y1 - y0),
                "mon_w": width,
                "mon_h": height,
            })
        return lineas


# ==============================================================================
# 3. VALIDADOR DE FORMATO PERFECTO XBOX (5x5) + AUTO-ZOOM 2X EN TIEMPO REAL
# ==============================================================================
# Alfabeto oficial de Xbox (Microsoft): 18 consonantes + 7 dígitos (NUNCA usa A, E, I, O, U, L, N, S, 0, 1, 5)
XBOX_VALID_CHARS = set("BCDFGHJKMPQRTVWXYZ2346789")

TABLA_CORRECCION_XBOX = str.maketrans({
    "0": "Q",
    "O": "D",
    "1": "7",
    "I": "J",
    "L": "T",
    "5": "6",
    "S": "9",
    "A": "4",
    "E": "F",
    "U": "V",
    "N": "M",
})

# Patrón 5x5: 5 bloques de 5 caracteres (tolera 1 carácter extra en los extremos por bordes de burbujas de chat)
PATRON_XBOX_5X5 = re.compile(
    r"([A-Z0-9]{5,6})[\s\-–—._•:;|/\\]+([A-Z0-9]{5})[\s\-–—._•:;|/\\]+([A-Z0-9]{5})[\s\-–—._•:;|/\\]+([A-Z0-9]{5})[\s\-–—._•:;|/\\]+([A-Z0-9]{5,6})",
    re.IGNORECASE,
)

# Detecta si una línea contiene bloques de código con 1 carácter borroso (ej. '*9P4X-7M2W8-...') para aplicar Zoom 2x
PATRON_SOSPECHA_CODIGO = re.compile(
    r"[A-Z0-9*#$!¡?¿]{3,6}[\s\-–—._]+[A-Z0-9]{5}[\s\-–—._]+[A-Z0-9]{5}",
    re.IGNORECASE,
)

# Tolera errores típicos del OCR: "eI", "e1", "e|", "códiqo", "2S"... Debe empezar la línea con "Escribe"
# para no confundirse con la descripción "...escribe tu código de 25 caracteres en el espacio siguiente".
RE_PLACEHOLDER_INPUT = re.compile(
    r"^\W*escrib\S*\s+e[lI1|!]\s+c\S*di\S*\s+de\s+2[5S]|enter\s+.*25-character",
    re.IGNORECASE,
)
RE_COMO_ESTE_BAJO_INPUT = re.compile(r"^(como\s+este|like\s+this)\s*:", re.IGNORECASE)
RE_ETIQUETA_ARRIBA_INPUT = re.compile(r"^se\s+agregar\S*\s+a\s+\S+@", re.IGNORECASE)
RE_BOTON_SIGUIENTE = re.compile(r"^(siguiente|sibuien\S*|siguien\S*|next)$", re.IGNORECASE)
RE_BOTON_CANCELAR = re.compile(r"^(cancelar|cancel)$", re.IGNORECASE)
RE_BOTON_CONFIRMAR = re.compile(r"^(confirmar|confirm|canjear\s+ahora|redeem\s+now)$", re.IGNORECASE)


def _validar_codigo_25(raw25: str, tiene_guiones: bool):
    """
    Valida que los 25 caracteres correspondan a un código de 25 caracteres (formato 5x5):
    - Rechaza texto común, palabras, frases o placeholders ('XXXXXXXXXXXXXXXXXXXXXXXXX').
    - Devuelve (codigo_25, codigo_formateado_5x5, es_100_puro_xbox).
    """
    raw25 = raw25.upper()
    if len(raw25) != 25 or not raw25.isalnum() or len(set(raw25)) <= 3:
        return None

    digit_count = sum(1 for c in raw25 if c.isdigit())
    alpha_count = sum(1 for c in raw25 if c.isalpha())
    valid_xbox_count = sum(1 for c in raw25 if c in XBOX_VALID_CHARS)

    if digit_count < 2 or alpha_count < 5:
        return None

    es_puro = valid_xbox_count == 25

    # Si viene con guiones explícitos 5x5, verificar que al menos 3 bloques tengan números
    if tiene_guiones and digit_count >= 3:
        bloques = [raw25[i : i + 5] for i in range(0, 25, 5)]
        bloques_con_digito = sum(1 for b in bloques if any(c.isdigit() for c in b))
        if bloques_con_digito >= 3:
            return raw25, "-".join(bloques), valid_xbox_count

    # Si no tenía todos los guiones pero cumple el alfabeto sin vocales de Xbox
    if valid_xbox_count >= 21 and digit_count >= 3:
        corregido = raw25.translate(TABLA_CORRECCION_XBOX)
        return corregido, "-".join(corregido[i : i + 5] for i in range(0, 25, 5)), valid_xbox_count

    return None


def _extraer_de_texto_linea(txt: str):
    txt = txt.upper()
    if any(m in txt for m in ("HTTP", "WWW.", ".COM", "UTM_", "=", "&", "?", "COPY-LINK", "@")):
        return []

    resultados = []
    for m in PATRON_XBOX_5X5.finditer(txt):
        g1, g2, g3, g4, g5 = m.groups()
        if len(g1) == 6:
            g1 = g1[-5:]
        if len(g5) == 6:
            g5 = g5[:5]
        raw25 = g1 + g2 + g3 + g4 + g5
        tiene_guion = any(c in txt[m.start() : m.end()] for c in "-–—._")
        res = _validar_codigo_25(raw25, tiene_guiones=tiene_guion)
        if res:
            resultados.append(res)

    if resultados:
        return resultados

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
                if res:
                    resultados.append(res)
                break
            elif len(acum) > 25:
                break

    return resultados


def detectar_codigos_en_lineas(lineas, casilla_xbox_coord=None, ocr=None, frame_bgra=None, fw=0, fh=0):
    encontrados = []
    vistos = set()

    for item in lineas:
        txt = item["text"]
        cx, cy = item["cx"], item["cy"]
        if RE_LINEA_DE_LOG.search(txt):
            continue  # Es la consola de este mismo script
        mon_w = item.get("mon_w", 1920)
        mon_h = item.get("mon_h", 1080)

        # Ignorar el texto que ya está escrito dentro de la propia casilla de Xbox (proporcional a la resolución)
        if casilla_xbox_coord:
            tol_x = max(220, int(mon_w * 0.14))
            tol_y = max(38, int(mon_h * 0.04))
            if abs(cx - casilla_xbox_coord[0]) < tol_x and abs(cy - casilla_xbox_coord[1]) < tol_y:
                continue

        extraidos = _extraer_de_texto_linea(txt)
        necesita_zoom = (
            not extraidos
            or (item["h"] < 20 and any(score < 25 for _, _, score in extraidos))
        )

        # Auto-Zoom 2x/3x BICUBIC sobre la línea cuando 1x falló o el texto es pequeño y tiene algún carácter dudoso
        if necesita_zoom and ocr and frame_bgra and (extraidos or PATRON_SOSPECHA_CODIGO.search(txt)):
            pad_x = max(12, int(item["h"] * 0.8))
            pad_y = max(8, int(item["h"] * 0.6))
            rx = max(0, item["rx"] - pad_x)
            ry = max(0, item["ry"] - pad_y)
            rw = min(fw - rx, item["w"] + pad_x * 2)
            rh = min(fh - ry, item["h"] + pad_y * 2)
            if rw > 20 and rh > 8:
                img_full = Image.frombytes("RGBA", (fw, fh), frame_bgra)
                crop = img_full.crop((rx, ry, rx + rw, ry + rh))
                factor = 3 if item["h"] < 15 else 2
                crop_zoom = crop.resize((rw * factor, rh * factor), Image.Resampling.BICUBIC)
                txt_zoom = ocr.recognize_text(crop_zoom.tobytes(), rw * factor, rh * factor)
                extraidos_zoom = _extraer_de_texto_linea(txt_zoom)
                if extraidos_zoom:
                    if not extraidos or extraidos_zoom[0][2] > extraidos[0][2]:
                        extraidos = extraidos_zoom

        for c25, cfmt, _ in extraidos:
            if c25 not in vistos:
                vistos.add(c25)
                encontrados.append((c25, cfmt, (cx, cy)))

    return encontrados


def localizar_ui_xbox_dinamica(lineas):
    """
    Localiza dinámicamente la casilla de Xbox y los botones SIGUIENTE y CONFIRMAR
    adaptando las distancias proporcionalmente al tamaño de fuente/resolución de cualquier pantalla.
    """
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
                offset_y = max(32, int(item["h"] * 3.8))
                offset_x = max(80, int(item["w"] * 0.5))
                coord_input = (item["x"] + offset_x, item["y"] - offset_y)
                break
            if RE_ETIQUETA_ARRIBA_INPUT.search(item["text"]):
                offset_y = max(28, int(item["h"] * 3.2))
                offset_x = max(80, int(item["w"] * 0.45))
                coord_input = (item["x"] + offset_x, item["y"] + item["h"] + offset_y)

    for item in lineas:
        limpio = re.sub(r"[^A-Za-zÁÉÍÓÚáéíóúñÑ\s]", "", item["text"]).strip()
        if not limpio or item["w"] > max(420, int(item.get("mon_w", 1920) * 0.25)) or item["h"] > 140:
            continue
        if RE_BOTON_CONFIRMAR.match(limpio):
            coord_confirmar = (item["cx"], item["cy"])
        elif RE_BOTON_SIGUIENTE.match(limpio):
            coord_siguiente = (item["cx"], item["cy"])
        elif RE_BOTON_CANCELAR.match(limpio):
            linea_cancelar = item

    if not coord_siguiente and linea_cancelar:
        coord_siguiente = (
            linea_cancelar["cx"] + int(linea_cancelar["w"] * 3.8),
            linea_cancelar["cy"],
        )

    return coord_input, coord_siguiente, coord_confirmar


# ==============================================================================
# 4. BUCLE MULTI-MONITOR (1, 2 O 3 MONITORES EN TIEMPO REAL)
# ==============================================================================
def obtener_monitores_activos(sct, max_monitores: int = 3):
    """Devuelve hasta 3 monitores físicos conectados (excluyendo el índice 0 virtual combinado)."""
    todos = sct.monitors[1 : max_monitores + 1]
    return list(enumerate(todos, start=1))


def _guardar_diagnostico(s, lineas, etiqueta: str):
    """Guarda la captura y el texto leído por el OCR en la carpeta debug/."""
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
    print("=" * 78)
    print("  🚀 SNIPER XBOX PRO (MULTI-MONITOR 1-3 PANTALLAS | 720P A 4K | AUTO-ZOOM)")
    print("=" * 78)

    # ---------- Validaciones de arranque ----------
    log("INFO", f"Python {sys.version.split()[0]} | DPI: {DPI_MODO}")
    try:
        es_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        es_admin = None
    log("INFO", f"Ejecutando como administrador: {es_admin}")
    log("INFO", f"Pantalla principal: {user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)} | "
                f"escritorio virtual: {user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)}x{user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)} "
                f"desde ({user32.GetSystemMetrics(SM_XVIRTUALSCREEN)},{user32.GetSystemMetrics(SM_YVIRTUALSCREEN)}) | "
                f"monitores según Windows: {user32.GetSystemMetrics(SM_CMONITORS)}")

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
    ocr.recognize_text(bytes(64 * 64 * 4), 64, 64)
    copiar_portapapeles_win32("")

    sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
    monitores = obtener_monitores_activos(sct, max_monitores=3)
    num_monitores_prev = len(monitores)

    print(f"  • Monitores detectados ({num_monitores_prev}):")
    for idx, m in monitores:
        principal = " [PRINCIPAL]" if m["left"] == 0 and m["top"] == 0 else ""
        print(f"    - Monitor {idx}: {m['width']}x{m['height']} en (X={m['left']}, Y={m['top']}) "
              f"proporción {m['width'] / m['height']:.2f}:1{principal}")
    if len(sct.monitors) - 1 > 3:
        log("WARN", f"Hay {len(sct.monitors) - 1} monitores; solo se vigilan los 3 primeros.")
    print("  • Puedes tener el video/WhatsApp en un monitor y Xbox en otro, o en pantalla dividida.")
    print("=" * 78)

    codigos_procesados = set()
    avisos_vistos = set()
    ultima_coord_xbox = None
    ultimo_btn_siguiente = None
    monitor_xbox_idx = 1
    ultimo_raw_por_monitor = {}

    # Estadísticas para el estado periódico
    t_estado = time.perf_counter()
    frames = 0
    ms_ocr_total = 0.0
    lineas_por_monitor = {}
    errores = 0

    # Escaneo inicial en todos los monitores para localizar la ventana de Xbox e ignorar códigos viejos
    pistas = []
    for idx, mon in monitores:
        try:
            t = time.perf_counter()
            s_init = sct.grab(mon)
            lin_init = ocr.recognize_monitor(s_init.raw, s_init.width, s_init.height, mon["left"], mon["top"])
            inp_i, sig_i, conf_i = localizar_ui_xbox_dinamica(lin_init)
            log("INFO", f"Prueba OCR monitor {idx}: {len(lin_init)} líneas en {(time.perf_counter() - t) * 1000:.0f} ms | "
                        f"casilla: {inp_i or 'no'} | siguiente: {sig_i} | confirmar: {conf_i}")
            if inp_i:
                ultima_coord_xbox = inp_i
                ultimo_btn_siguiente = sig_i
                monitor_xbox_idx = idx
                log("INFO", f"🎯 CASILLA XBOX LISTA EN MONITOR {idx} | Posición: {ultima_coord_xbox} | Siguiente: {ultimo_btn_siguiente}")
            pistas += [it["text"] for it in lin_init
                       if re.search(r"c.digo|25|caracteres|character|canjea|redeem", it["text"], re.I)
                       and not RE_LINEA_DE_LOG.search(it["text"])]
            _guardar_diagnostico(s_init, lin_init, f"arranque_monitor{idx}")
        except Exception as e:
            log("ERROR", f"Falló el escaneo inicial del monitor {idx}: {e!r}")
            if DEBUG:
                traceback.print_exc()

    if not ultima_coord_xbox:
        log("WARN", "Casilla de Xbox NO ENCONTRADA en ningún monitor. Abre xbox.com/redeem y deja visible "
                    "'Escribe el código de 25 caracteres'.")
        log("WARN", f"Textos parecidos a la página de Xbox vistos por el OCR: {pistas[:8] or 'ninguno'}")

    print()
    log("INFO", "✔ Vigilando todos tus monitores en tiempo real... (Ctrl+C para detener)\n")

    while True:
        try:
            # Detectar en caliente si se conectó o desconectó un monitor (hasta 3 máx)
            n_actual = min(user32.GetSystemMetrics(SM_CMONITORS), 3)
            if n_actual != num_monitores_prev:
                try:
                    sct.close()
                except Exception:
                    pass
                sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
                monitores = obtener_monitores_activos(sct, max_monitores=3)
                num_monitores_prev = len(monitores)
                ultimo_raw_por_monitor.clear()
                log("INFO", f"🔄 Configuración de pantallas actualizada: {num_monitores_prev} monitor(es) activos.")

            for idx, mon in monitores:
                s = sct.grab(mon)
                if ultimo_raw_por_monitor.get(idx) == s.raw:
                    continue

                ultimo_raw_por_monitor[idx] = s.raw
                t_frame = time.perf_counter()

                lineas = ocr.recognize_monitor(s.raw, s.width, s.height, mon["left"], mon["top"])
                frames += 1
                ms_ocr_total += (time.perf_counter() - t_frame) * 1000.0
                lineas_por_monitor[idx] = len(lineas)
                dyn_input, dyn_sig, _ = localizar_ui_xbox_dinamica(lineas)

                if dyn_input:
                    if not ultima_coord_xbox or (
                        abs(dyn_input[0] - ultima_coord_xbox[0]) > 14 or abs(dyn_input[1] - ultima_coord_xbox[1]) > 14
                    ):
                        log("INFO", f"🎯 CASILLA XBOX EN MONITOR {idx} | Posición: {dyn_input} | Botón Siguiente: {dyn_sig}")
                    ultima_coord_xbox = dyn_input
                    monitor_xbox_idx = idx
                if dyn_sig and monitor_xbox_idx == idx:
                    ultimo_btn_siguiente = dyn_sig

                codigos = detectar_codigos_en_lineas(
                    lineas,
                    casilla_xbox_coord=ultima_coord_xbox if monitor_xbox_idx == idx else None,
                    ocr=ocr,
                    frame_bgra=s.raw,
                    fw=s.width,
                    fh=s.height,
                )
                validos = {c[0] for c in codigos}

                # Textos con pinta de código que no pasaron el filtro (solo mayúsculas/números, sin la propia consola)
                for it in lineas:
                    if RE_LINEA_DE_LOG.search(it["text"]):
                        continue
                    compacto = re.sub(r"[^A-Za-z0-9]", "", it["text"])
                    if len(compacto) < 20 or it["text"] in avisos_vistos:
                        continue
                    mayus = sum(1 for c in compacto if c.isupper() or c.isdigit())
                    if mayus / len(compacto) >= 0.8 and not any(
                        v in compacto.upper() or v in compacto.upper().translate(TABLA_CORRECCION_XBOX) for v in validos
                    ):
                        avisos_vistos.add(it["text"])
                        log("DBG", f"Texto tipo código rechazado en monitor {idx} ({it['cx']},{it['cy']}): "
                                   f"empieza '{compacto[:5]}…' ({len(compacto)} alfanum.)")

                for c25, cfmt, pos_codigo in reversed(codigos):
                    if c25 in codigos_procesados:
                        continue
                    if not ultima_coord_xbox:
                        if c25 not in avisos_vistos:
                            avisos_vistos.add(c25)
                            log("WARN", f"Código {enmascarar(cfmt)} visto en monitor {idx} {pos_codigo}, pero NO se ve la "
                                        "casilla de Xbox en ningún monitor. No se escribe. Revisa debug/.")
                            _guardar_diagnostico(s, lineas, "codigo_sin_casilla")
                        continue

                    codigos_procesados.add(c25)
                    log("INFO", f"Código {enmascarar(cfmt)} visto en monitor {idx} {pos_codigo}. "
                                f"Pegando en casilla {ultima_coord_xbox} (monitor {monitor_xbox_idx})...")
                    n_ev = inyectar_codigo_en_xbox(ultima_coord_xbox, c25)
                    ms = (time.perf_counter() - t_frame) * 1000.0
                    log("INFO", f"⚡ MONITOR {idx} -> MONITOR {monitor_xbox_idx} en {ms:.1f} ms (eventos={n_ev}): {enmascarar(cfmt)}")

                    # Fase rápida de confirmación escaneando únicamente el monitor donde está Xbox
                    mon_xbox = dict(monitores).get(monitor_xbox_idx, mon)
                    confirmado = False
                    s2, lin2 = s, lineas
                    for intento in range(22):
                        time.sleep(0.12)
                        s2 = sct.grab(mon_xbox)
                        lin2 = ocr.recognize_monitor(
                            s2.raw, s2.width, s2.height, mon_xbox["left"], mon_xbox["top"]
                        )
                        _, sig2, conf2 = localizar_ui_xbox_dinamica(lin2)
                        log("DBG", f"  Intento {intento + 1}/22: Siguiente={sig2} Confirmar={conf2}")
                        if conf2:
                            clic_multimonitor(conf2[0], conf2[1])
                            presionar_enter_nativo()
                            log("INFO", f"  ✔ AUTO-CONFIRMAR: botón 'CONFIRMAR' pulsado en {conf2}")
                            confirmado = True
                            break
                        target_sig = sig2 or ultimo_btn_siguiente
                        if target_sig:
                            clic_multimonitor(target_sig[0], target_sig[1])
                        elif intento == 0:
                            log("WARN", "  No se encontró el botón SIGUIENTE ni CANCELAR en pantalla.")
                    if not confirmado:
                        log("WARN", "  No apareció el botón CONFIRMAR tras 22 intentos.")
                        _guardar_diagnostico(s2, lin2, "sin_confirmar")

                    try:
                        winsound.MessageBeep(winsound.MB_ICONASTERISK)
                    except Exception:
                        pass
                    ultimo_raw_por_monitor.clear()
                    break

            # Estado periódico
            if time.perf_counter() - t_estado >= INTERVALO_ESTADO_SEG:
                prom = ms_ocr_total / frames if frames else 0
                log("DBG", f"Estado: {frames} frames, OCR prom {prom:.0f} ms | líneas por monitor: {lineas_por_monitor} | "
                           f"casilla: {ultima_coord_xbox} (monitor {monitor_xbox_idx}) | "
                           f"siguiente: {ultimo_btn_siguiente} | errores: {errores}")
                t_estado = time.perf_counter()
                frames, ms_ocr_total = 0, 0.0

            time.sleep(0.003)

        except KeyboardInterrupt:
            print()
            log("INFO", "⏹ Sniper detenido.")
            break
        except Exception as e:
            errores += 1
            if errores <= 5 or errores % 50 == 0:
                log("ERROR", f"Excepción #{errores} en el bucle: {e!r}")
                if DEBUG:
                    traceback.print_exc()
            time.sleep(0.015)


if __name__ == "__main__":
    main()
