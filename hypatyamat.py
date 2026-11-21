"""
Organização:
  1) Configuração (tamanho da janela, pastas das imagens)
  2) Funções do Windows (admin, rato)
  3) Câmara do ecrã
  4) Ferramentas de imagem (procurar formas)
  5) Leitor do Resultado
  6) Leitor do Sinal
  7) Leitor da Grelha
  8) Escolher jogada + clicar com cuidado
  9) Programa principal (janela + ciclo infinito)
"""


from __future__ import annotations

import ctypes
import ctypes.wintypes
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import cv2
import numpy as np
from mss import MSS
from rapidocr_onnxruntime import RapidOCR

#Configuração de FPS
FPS = 30  

# 'Limiar' = nota mínima (0 a 1) para aceitar uma correspondência de imagem.
# Mais alto = mais exigente (menos falsos positivos, pode falhar mais).
LIMIAR_BALAO = 0.78
LIMIAR_REFINO = 0.88
LIMIAR_DIGITO = 0.62
LIMIAR_DIGITO_GRELHA = 0.72

ESCALAS_BALAO = (0.65, 0.9, 1.2, 1.6)  # tamanhode procura do balão
ESCALAS_DIGITOS = (1.2, 1.8, 2.5, 3.2)  # tamanhode procura de dígitos

# Pasta com as imagens de exemplo que o programa compara com o ecrã
ASSETS = Path(__file__).resolve().parent / "assets" / "assets_nomeados"
TEMPLATE_BALAO = ASSETS / "balao_verde.png"  # foto do balão verde
TEMPLATE_JOGAR = ASSETS / "botao_jogar.png"  # botão laranja com a palavra Jogar
GRELHA_TPL = ASSETS / "grelha_tpl"  # fotos dos dígitos 0-9 da grelha

# REGISTO DE DEBUG (podes ignorar no dia-a-dia)
# Guarda num ficheiro .log o que o programa pensou em certos momentos,
# para descobrir bugs. Não afeta o jogo.
# #region agent log
_DEBUG_LOG = Path(__file__).resolve().parent / "Debug" / "debug-4ffa70.log"


def _dbg(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    # Escreve uma linha de diagnóstico no ficheiro de log (falhas são silenciadas).
    import json

    try:
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessionId": "4ffa70",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "timestamp": int(time.time() * 1000),
        }
        with _DEBUG_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


# #endregion


# FUNÇÕES DE SISTEMA (Windows)
# Pedir admin, esconder a janela preta da consola, mover o rato e clicar.
VK_F6 = 0x75  # tecla F6 — trava de segurança (pausa / continua)
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000  # o Windows não repete o F6 enquanto seguras


def _tecla_premida(vk: int) -> bool:
    # True se a tecla está premida AGORA (funciona mesmo com o foco noutro programa).
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _vigilante_f6(
    parar: threading.Event,
    pausado: threading.Event,
    pedido_reinicio: threading.Event,
    ao_mudar,
) -> None:
    # Fio só para o F6: o Windows avisa-nos mesmo enquanto o bot está a ler/clicar.
    # Assim a pausa não falha se carregares F6 a meio de uma jogada.
    user32 = ctypes.windll.user32
    id_hotkey = 1
    registrou = bool(user32.RegisterHotKey(None, id_hotkey, MOD_NOREPEAT, VK_F6))

    def _alternar() -> None:
        if pausado.is_set():
            pausado.clear()
        else:
            pausado.set()
        pedido_reinicio.set()
        try:
            ao_mudar(pausado.is_set())
        except Exception:
            pass

    try:
        if registrou:
            msg = ctypes.wintypes.MSG()
            while not parar.is_set():
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == WM_HOTKEY and msg.wParam == id_hotkey:
                        _alternar()
                parar.wait(0.02)
        else:
            # Plano B: se o F6 já estiver ocupado no Windows, verifica 50×/segundo
            f6_antes = False
            while not parar.is_set():
                f6_agora = _tecla_premida(VK_F6)
                if f6_agora and not f6_antes:
                    _alternar()
                f6_antes = f6_agora
                parar.wait(0.02)
    finally:
        if registrou:
            user32.UnregisterHotKey(None, id_hotkey)


def _python_sem_cmd() -> str:
    # Prefere pythonw.exe (sem janela preta de consola) se existir.
    executavel = Path(sys.executable)
    if executavel.name.lower() == "python.exe":
        candidato = executavel.with_name("pythonw.exe")
        if candidato.exists():
            return str(candidato)
    return str(executavel)


def pedir_admin() -> None:
    # Se não formos administrador, pede elevação UAC e reinicia o programa.
    # Sem admin o Windows muitas vezes bloqueia mover o rato noutros programas.
    if ctypes.windll.shell32.IsUserAnAdmin():
        return

    resultado = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        _python_sem_cmd(),
        f'"{Path(__file__).resolve()}"',
        None,
        1,
    )
    if resultado <= 32:
        raise SystemExit(f"UAC recusado ou falhou (codigo {resultado}).")
    raise SystemExit(0)


def esconder_cmd() -> None:
    # Esconde a janela preta da consola (cmd), para não atrapalhar o ecrã.
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)


def mover_rato(x: int, y: int) -> None:
    # Põe o ponteiro do rato nas coordenadas (x, y) do ecrã.
    user32 = ctypes.windll.user32
    xi, yi = int(x), int(y)
    if not user32.SetCursorPos(xi, yi):
        # retry uma vez (por vezes falha em multi-monitor)
        time.sleep(0.01)
        if not user32.SetCursorPos(xi, yi):
            erro = ctypes.get_last_error()
            raise RuntimeError(f"SetCursorPos falhou (erro {erro}).")
    time.sleep(0.01)


def clicar_esquerdo() -> None:
    # Carrega e larga o botão esquerdo do rato (um clique normal).
    user32 = ctypes.windll.user32
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.03)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.02)


# CÂMARA DO ECRÃ
# Tira "fotografias" de tudo o que está no(s) monitor(es), muitas vezes/segundo.
class VisaoEcra:
    # Tira capturas do ecrã inteiro (todos os monitores juntos).
    def __init__(self) -> None:
        self._mss = MSS()
        self._regiao = self._mss.monitors[0]
        self.frame: np.ndarray | None = None

    @property
    def origem(self) -> tuple[int, int]:
        return int(self._regiao["left"]), int(self._regiao["top"])

    def ver(self) -> np.ndarray:
        # Fotografa o ecrã agora e guarda em self.frame.
        captura = self._mss.grab(self._regiao)
        self.frame = np.asarray(captura, dtype=np.uint8)
        return self.frame

    def fechar(self) -> None:
        self._mss.close()


# FERRAMENTAS DE IMAGEM
# Carregar fotos de exemplo, procurar um desenho no ecrã, comparar silhuetas,
# detetar vermelho, etc. São peças usadas pelos leitores mais abaixo.
def carregar_rgba(caminho: Path) -> tuple[np.ndarray, np.ndarray]:
    # Carrega PNG (com transparência) → (cores BGR, máscara do que é 'peça').
    img = cv2.imread(str(caminho), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(f"Nao encontrei o template: {caminho}")
    if img.ndim == 2:
        bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        mascara = np.full(img.shape, 255, np.uint8)
    elif img.shape[2] == 4:
        bgr = img[:, :, :3]
        mascara = img[:, :, 3]
    else:
        bgr = img[:, :, :3]
        cinza = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        mascara = np.where(cinza > 15, 255, 0).astype(np.uint8)
    modelo = bgr.copy()
    modelo[mascara < 20] = 0
    return modelo, mascara


def _match_bgr(
    # Compara um desenho pequeno (modelo) com o ecrã: quão parecido é + onde está.
    tela_bgr: np.ndarray,
    modelo_bgr: np.ndarray,
    mascara: np.ndarray | None = None,
) -> tuple[float, tuple[int, int]]:
    if (
        modelo_bgr.shape[0] >= tela_bgr.shape[0]
        or modelo_bgr.shape[1] >= tela_bgr.shape[1]
        or modelo_bgr.size == 0
    ):
        return -1.0, (0, 0)
    if mascara is None:
        resultado = cv2.matchTemplate(tela_bgr, modelo_bgr, cv2.TM_CCOEFF_NORMED)
    else:
        mask = (mascara > 20).astype(np.uint8) * 255
        if mask.shape[:2] != modelo_bgr.shape[:2]:
            return -1.0, (0, 0)
        resultado = cv2.matchTemplate(
            tela_bgr,
            modelo_bgr,
            cv2.TM_CCORR_NORMED,
            mask=mask,
        )
    _, max_val, _, max_loc = cv2.minMaxLoc(resultado)
    conf = float(max_val)
    if not np.isfinite(conf):
        return -1.0, (0, 0)
    return conf, (int(max_loc[0]), int(max_loc[1]))


def _e_verde_lima(tela_bgr: np.ndarray, cx: int, cy: int, w: int, h: int) -> bool:
    # Confirma se o centro do balão é mesmo verde-lima (evita cliques errados).
    x0 = max(0, cx - w // 2)
    y0 = max(0, cy - h // 2)
    x1 = min(tela_bgr.shape[1], x0 + w)
    y1 = min(tela_bgr.shape[0], y0 + h)
    zona = tela_bgr[y0:y1, x0:x1]
    if zona.size == 0:
        return False
    hsv = cv2.cvtColor(zona, cv2.COLOR_BGR2HSV)
    lima = cv2.inRange(hsv, np.array((52, 80, 140)), np.array((74, 255, 255)))
    return float(lima.mean()) / 255.0 >= 0.35


def encontrar_balao(
    # Procura o balão verde no ecrã; se achar bem, devolve (centro_x, centro_y, confiança).
    frame_bgra: np.ndarray,
    template_bgr: np.ndarray,
    mascara: np.ndarray,
    limiar: float = LIMIAR_BALAO,
) -> tuple[int, int, float] | None:
    tela = np.ascontiguousarray(frame_bgra[:, :, :3])
    base = template_bgr.copy()
    base[mascara < 20] = 0
    melhor: tuple[int, int, float, int, int] | None = None

    fator = 0.4
    tela_p = cv2.resize(tela, None, fx=fator, fy=fator, interpolation=cv2.INTER_AREA)
    for escala in ESCALAS_BALAO:
        w = max(12, int(base.shape[1] * escala * fator))
        h = max(12, int(base.shape[0] * escala * fator))
        if h >= tela_p.shape[0] or w >= tela_p.shape[1]:
            continue
        modelo = cv2.resize(base, (w, h), interpolation=cv2.INTER_AREA)
        mask_p = cv2.resize(mascara, (w, h), interpolation=cv2.INTER_NEAREST)
        conf, (x, y) = _match_bgr(tela_p, modelo, mask_p)
        if conf < limiar:
            continue
        x0 = max(0, int(x / fator) - w)
        y0 = max(0, int(y / fator) - h)
        x1 = min(tela.shape[1], int((x + w) / fator) + w)
        y1 = min(tela.shape[0], int((y + h) / fator) + h)
        recorte = tela[y0:y1, x0:x1]
        wr = max(12, int(base.shape[1] * escala))
        hr = max(12, int(base.shape[0] * escala))
        if hr >= recorte.shape[0] or wr >= recorte.shape[1]:
            continue
        modelo_r = cv2.resize(base, (wr, hr), interpolation=cv2.INTER_AREA)
        mask_r = cv2.resize(mascara, (wr, hr), interpolation=cv2.INTER_NEAREST)
        conf_r, (xr, yr) = _match_bgr(recorte, modelo_r, mask_r)
        if conf_r < LIMIAR_REFINO:
            continue
        cx = x0 + xr + wr // 2
        cy = y0 + yr + hr // 2
        if not _e_verde_lima(tela, cx, cy, wr, hr):
            continue
        if melhor is None or conf_r > melhor[2]:
            melhor = (x0 + xr, y0 + yr, conf_r, wr, hr)
        if conf_r >= 0.96:
            break

    if melhor is None:
        return None
    x, y, conf, w, h = melhor
    return x + w // 2, y + h // 2, conf


def _relogio_no_canto(
    tela: np.ndarray,
    circulo: tuple[int, int, int, int],
    relogio_bgr: np.ndarray,
    relogio_m: np.ndarray,
) -> bool:
    # O ícone do relógio fica no canto superior esquerdo do círculo do tempo.
    x, y, w, h = circulo
    x0 = max(0, x - int(w * 0.55))
    y0 = max(0, y - int(h * 0.55))
    x1 = min(tela.shape[1], x + int(w * 0.6))
    y1 = min(tela.shape[0], y + int(h * 0.6))
    zona = tela[y0:y1, x0:x1]
    if zona.size == 0:
        return False
    for escala in (0.35, 0.55, 0.8, 1.1, 1.5, 2.0, 2.6):
        wr = max(10, int(relogio_bgr.shape[1] * escala))
        hr = max(10, int(relogio_bgr.shape[0] * escala))
        if hr >= zona.shape[0] or wr >= zona.shape[1]:
            continue
        if wr > w * 0.9 or hr > h * 0.9:
            continue
        modelo = cv2.resize(relogio_bgr, (wr, hr), interpolation=cv2.INTER_AREA)
        mask_r = cv2.resize(relogio_m, (wr, hr), interpolation=cv2.INTER_NEAREST)
        conf, _loc = _match_bgr(zona, modelo, mask_r)
        if conf >= 0.72:
            return True
    return False


def _centro_e_zero(circulo_bgr: np.ndarray) -> bool:
    # No meio do círculo vermelho há um 0 preto (anel escuro com um buraco).
    h, w = circulo_bgr.shape[:2]
    m = max(4, int(min(h, w) * 0.22))
    if h - 2 * m < 8 or w - 2 * m < 8:
        return False
    centro = circulo_bgr[m : h - m, m : w - m]
    valor = cv2.cvtColor(centro, cv2.COLOR_BGR2HSV)[:, :, 2]
    escuro = np.where(valor < 80, 255, 0).astype(np.uint8)
    if int(escuro.sum()) < 400:
        return False
    inv = cv2.bitwise_not(escuro)
    ff = inv.copy()
    flood = np.zeros((ff.shape[0] + 2, ff.shape[1] + 2), np.uint8)
    cv2.floodFill(ff, flood, (0, 0), 128)
    buraco = int((ff == 255).sum())
    return buraco >= 25


def tempo_e_zero(
    tela_bgr: np.ndarray,
    relogio_bgr: np.ndarray,
    relogio_m: np.ndarray,
) -> bool:
    # O tempo a 0 é o círculo vermelho com o 0 preto e o relógio no canto.
    # Procura só nessa zona e em meia resolução: varrer o ecrã inteiro atrasava a jogada.
    tela = np.ascontiguousarray(tela_bgr[:, :, :3])
    fator = 0.5
    canto = tela[: int(tela.shape[0] * 0.7), : int(tela.shape[1] * 0.6)]
    if canto.size == 0:
        return False
    pequena = cv2.resize(canto, None, fx=fator, fy=fator, interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(pequena, cv2.COLOR_BGR2HSV)
    vermelho = cv2.bitwise_or(
        cv2.inRange(hsv, (0, 140, 140), (12, 255, 255)),
        cv2.inRange(hsv, (170, 140, 140), (179, 255, 255)),
    )
    vermelho = cv2.morphologyEx(vermelho, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contornos, _ = cv2.findContours(vermelho, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    area_ecra = tela.shape[0] * tela.shape[1]
    min_area = max(1800, int(area_ecra * 0.002)) * fator * fator
    max_area = 450_000 * fator * fator
    for contorno in contornos:
        x, y, w, h = cv2.boundingRect(contorno)
        area = w * h
        if area < min_area or area > max_area:
            continue
        if not (0.72 <= w / max(h, 1) <= 1.4):
            continue
        peri = cv2.arcLength(contorno, True)
        if peri <= 0:
            continue
        circ = 4 * np.pi * cv2.contourArea(contorno) / (peri * peri)
        if circ < 0.5:
            continue
        x0, y0, w0, h0 = (int(round(v / fator)) for v in (x, y, w, h))
        x0 = max(0, min(canto.shape[1] - 1, x0))
        y0 = max(0, min(canto.shape[0] - 1, y0))
        w0 = max(1, min(canto.shape[1] - x0, w0))
        h0 = max(1, min(canto.shape[0] - y0, h0))
        if not _relogio_no_canto(canto, (x0, y0, w0, h0), relogio_bgr, relogio_m):
            continue
        if _centro_e_zero(canto[y0 : y0 + h0, x0 : x0 + w0]):
            return True
    return False


def encontrar_fila_setas(
    tela_bgr: np.ndarray,
    fila_bgr: np.ndarray,
) -> tuple[int, int] | None:
    # Procura a foto inteira da fila (as cinco setas juntas) e devolve o centro.
    tela = np.ascontiguousarray(tela_bgr[:, :, :3])
    fila = np.ascontiguousarray(fila_bgr[:, :, :3])
    fator = 0.5
    tela_p = cv2.resize(tela, None, fx=fator, fy=fator, interpolation=cv2.INTER_AREA)
    melhor: tuple[float, int, int] | None = None
    escala = 0.35
    while escala <= 3.2:
        wr = int(fila.shape[1] * escala * fator)
        hr = int(fila.shape[0] * escala * fator)
        escala = round(escala + 0.08, 3)
        if hr < 12 or wr < 20 or hr >= tela_p.shape[0] or wr >= tela_p.shape[1]:
            continue
        modelo = cv2.resize(fila, (wr, hr), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(tela_p, modelo, cv2.TM_CCOEFF_NORMED)
        _min, max_val, _min_loc, max_loc = cv2.minMaxLoc(res)
        conf = float(max_val)
        if not np.isfinite(conf) or conf < 0.58:
            continue
        cx = int(round((max_loc[0] + wr / 2) / fator))
        cy = int(round((max_loc[1] + hr / 2) / fator))
        if melhor is None or conf > melhor[0]:
            melhor = (conf, cx, cy)
        if conf >= 0.90:
            break
    if melhor is None:
        return None
    return melhor[1], melhor[2]


def encontrar_botao_jogar(
    tela_bgr: np.ndarray,
    botao_bgr: np.ndarray,
) -> tuple[int, int] | None:
    # Pastilha laranja com a palavra «Jogar» a vermelho.
    # Os outros botões amarelos não têm esse texto, por isso não são clicados.
    # A forma procura-se em metade da resolução; a confirmação é no recorte real.
    tela = np.ascontiguousarray(tela_bgr[:, :, :3])
    fator = 0.5
    pequena = cv2.resize(tela, None, fx=fator, fy=fator, interpolation=cv2.INTER_AREA)
    hsv_p = cv2.cvtColor(pequena, cv2.COLOR_BGR2HSV)
    laranja = cv2.inRange(hsv_p, (12, 110, 170), (34, 255, 255))
    laranja = cv2.morphologyEx(laranja, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    contornos, _ = cv2.findContours(laranja, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    th, tw = botao_bgr.shape[:2]
    melhor: tuple[float, int, int] | None = None
    altura, largura = tela.shape[:2]
    for contorno in contornos:
        xs, ys, ws, hs = cv2.boundingRect(contorno)
        x = int(round(xs / fator))
        y = int(round(ys / fator))
        w = int(round(ws / fator))
        h = int(round(hs / fator))
        x = max(0, min(largura - 1, x))
        y = max(0, min(altura - 1, y))
        w = max(1, min(largura - x, w))
        h = max(1, min(altura - y, h))
        area = w * h
        # Pequeno demais é ruído; grande demais não é este botão.
        if area < 1600 or w > 1100 or h > 420:
            continue
        if not (2.05 <= w / max(h, 1) <= 3.9):
            continue
        if cv2.contourArea(contorno) < (ws * hs) * 0.62:
            continue
        recorte = tela[y : y + h, x : x + w]
        if recorte.size == 0:
            continue
        hsv_r = cv2.cvtColor(recorte, cv2.COLOR_BGR2HSV)
        vermelho = cv2.bitwise_or(
            cv2.inRange(hsv_r, (0, 120, 60), (14, 255, 255)),
            cv2.inRange(hsv_r, (165, 120, 60), (179, 255, 255)),
        )
        if int((vermelho > 0).sum()) < 40:
            continue
        if float(vermelho.mean()) / 255.0 < 0.012:
            continue
        interp = cv2.INTER_AREA if (w >= tw and h >= th) else cv2.INTER_LINEAR
        comparado = cv2.resize(recorte, (tw, th), interpolation=interp)
        nota = float(cv2.matchTemplate(comparado, botao_bgr, cv2.TM_CCOEFF_NORMED)[0, 0])
        if not np.isfinite(nota) or nota < 0.72:
            continue
        if melhor is None or nota > melhor[0]:
            melhor = (nota, x + w // 2, y + h // 2)
    if melhor is None:
        return None
    return melhor[1], melhor[2]


def _mascara_vermelha(bgr: np.ndarray) -> np.ndarray:
    # Branco = píxeis vermelhos (números, menos, molduras); preto = resto.
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (0, 60, 60), (16, 255, 255))
    m2 = cv2.inRange(hsv, (155, 60, 60), (179, 255, 255))
    return cv2.bitwise_or(m1, m2)


def _hits_template(
    # Em vários tamanhos, encontra onde um dígito-modelo aparece no ecrã.
    tela: np.ndarray,
    modelo: np.ndarray,
    mascara: np.ndarray,
    escalas: tuple[float, ...],
    limiar: float,
) -> list[tuple[int, int, int, int, float]]:
    tela_r = _mascara_vermelha(tela)
    brutos: list[tuple[int, int, int, int, float]] = []
    for escala in escalas:
        wr = max(8, int(modelo.shape[1] * escala))
        hr = max(8, int(modelo.shape[0] * escala))
        if hr >= tela_r.shape[0] or wr >= tela_r.shape[1]:
            continue
        m = cv2.resize(modelo, (wr, hr), interpolation=cv2.INTER_AREA)
        mask = cv2.resize(mascara, (wr, hr), interpolation=cv2.INTER_NEAREST)
        dig = _mascara_vermelha(m)
        dig[mask < 20] = 0
        if int(dig.sum()) < 30:
            dig = np.where(mask > 20, 255, 0).astype(np.uint8)
        res = cv2.matchTemplate(tela_r, dig, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        conf = float(max_val)
        if np.isfinite(conf) and conf >= limiar:
            brutos.append((int(max_loc[0]), int(max_loc[1]), wr, hr, conf))

    brutos.sort(key=lambda t: t[4], reverse=True)
    escolhidos: list[tuple[int, int, int, int, float]] = []
    for cand in brutos:
        x, y, w, h, conf = cand
        cx, cy = x + w / 2, y + h / 2
        if any(
            abs(cx - (ex + ew / 2)) < max(w, ew) * 0.55
            and abs(cy - (ey + eh / 2)) < max(h, eh) * 0.55
            for ex, ey, ew, eh, _ in escolhidos
        ):
            continue
        escolhidos.append(cand)
    return escolhidos


def _norm_sil(sil: np.ndarray, size: tuple[int, int] = (32, 40)) -> np.ndarray | None:
    # Recorta a forma e põe-na num tamanho fixo para comparar com outras.
    ys, xs = np.where(sil > 0)
    if len(xs) < 8:
        return None
    crop = sil[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    return cv2.resize(crop, size, interpolation=cv2.INTER_AREA)


def _score_sil(a_img: np.ndarray, b_img: np.ndarray) -> float:
    # Nota 0..1: quão iguais são duas silhuetas (formas de dígitos).
    a = a_img.astype(np.float32) / 255.0
    b = b_img.astype(np.float32) / 255.0
    den = float(np.sqrt((a * a).sum() * (b * b).sum()) + 1e-6)
    corr = float((a * b).sum() / den)
    xor = 1.0 - float(np.mean((a_img > 127) != (b_img > 127)))
    return 0.5 * corr + 0.5 * xor


def _contar_buracos(sil: np.ndarray) -> int:
    # Quantos "buracos" tem a silhueta (anéis por dentro).
    # 2 → 0 buracos; 0/6/9 → 1; 8 → 2. Serve para não trocar 0↔8 nem 2↔9.
    n = _norm_sil(sil, (48, 60))
    if n is None:
        return 0
    binimg = np.where(n > 100, 255, 0).astype(np.uint8)
    pad = cv2.copyMakeBorder(binimg, 3, 3, 3, 3, cv2.BORDER_CONSTANT, value=0)
    _cnts, hier = cv2.findContours(pad, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return 0
    n_buracos = 0
    for i, cnt in enumerate(_cnts):
        # hierarquia: filho de outro contorno = buraco interior
        if hier[0][i][3] >= 0 and cv2.contourArea(cnt) >= 10:
            n_buracos += 1
    return n_buracos


# MATEMÁTICA SIMPLES
# Recebe dois números e um sinal (+ - * /) e devolve o resultado.
# Se a conta for inválida (ex.: dividir por zero), devolve "nada" (None).
def aplicar_op(a: int, op: str, c: int) -> int | None:
    # Faz a conta a ⊕ c. Divisão só se for exata (sem resto).
    # Para Resultado 0: só 0÷n (n≠0). n÷0 é inválido e nunca é devolvido.
    if op == "+":
        return a + c
    if op == "-":
        return a - c
    if op == "*":
        return a * c
    if op == "/":
        if c == 0 or a % c != 0:
            return None
        return a // c
    return None


# LEITOR DO "RESULTADO"
# 1) Procura o círculo amarelo no ecrã
# 2) Confirma que diz "Resultado" (ou parece)
# 3) Lê o número vermelho (ex.: 7 ou -7)
class LeitorResultado:
    # Encontra o círculo amarelo com "Resultado" e lê o número (pode ser negativo).

    def __init__(self) -> None:
        # Carrega OCR + fotos do menos e dos dígitos 0-9 (só a parte vermelha).
        self.ocr = RapidOCR()
        self.menos, self.menos_m = carregar_rgba(ASSETS / "sinal_menos.png")
        self.digitos: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.digitos_mask: dict[str, np.ndarray] = {}
        for d in "0123456789":
            caminho = ASSETS / f"numero_{d}.png"
            if caminho.exists():
                modelo, mask = carregar_rgba(caminho)
                self.digitos[d] = (modelo, mask)
                vermelho = _mascara_vermelha(modelo)
                vermelho[mask < 20] = 0
                if int(vermelho.sum()) < 20:
                    vermelho = np.where(mask > 20, 255, 0).astype(np.uint8)
                self.digitos_mask[d] = vermelho

    def _rois_circulo(self, tela: np.ndarray) -> list[tuple[int, int, int, int, float]]:
        # Procura manchas amarelas redondas (= candidatos a círculo "Resultado").
        hsv = cv2.cvtColor(tela, cv2.COLOR_BGR2HSV)
        amarelo = cv2.inRange(hsv, (12, 80, 140), (40, 255, 255))
        amarelo = cv2.morphologyEx(amarelo, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        contornos, _ = cv2.findContours(amarelo, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        rois: list[tuple[int, int, int, int, float]] = []
        for contorno in contornos:
            x, y, w, h = cv2.boundingRect(contorno)
            area = w * h
            if area < 8_000 or area > 200_000:
                continue
            razao = w / max(h, 1)
            if razao < 0.75 or razao > 1.35:
                continue
            peri = cv2.arcLength(contorno, True)
            if peri <= 0:
                continue
            circ = 4 * np.pi * cv2.contourArea(contorno) / (peri * peri)
            if circ < 0.55:
                continue
            bonus = 0.05 if x > tela.shape[1] * 0.45 and y < tela.shape[0] * 0.45 else 0.0
            rois.append((x, y, w, h, 0.80 + 0.1 * circ + bonus))
        rois.sort(key=lambda r: r[4], reverse=True)
        return rois[:4]

    def _tem_resultado_rapido(self, recorte_bgr: np.ndarray) -> bool:
        # Atalho: no topo do círculo há várias manchinhas vermelhas (letras)?
        h = recorte_bgr.shape[0]
        topo = recorte_bgr[: max(1, int(h * 0.45)), :]
        mask = _mascara_vermelha(topo)
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        letras = 0
        for i in range(1, n):
            _x, _y, w, hh, area = (int(v) for v in stats[i])
            if area < 25 or area > 500:
                continue
            if hh < 6 or w < 4:
                continue
            if hh > h * 0.28:
                continue
            letras += 1
        return letras >= 4

    def _tem_resultado_ocr(self, recorte_bgr: np.ndarray) -> bool:
        # Lê o texto na imagem e verifica se aparece "Resultado".
        try:
            out, _ = self.ocr(recorte_bgr)
        except Exception:
            return False
        if not out:
            return False
        for item in out:
            compacto = re.sub(r"[^a-z]", "", str(item[1]).casefold())
            if "resultad" in compacto or "resultado" in compacto:
                return True
            if "resul" in compacto and "tad" in compacto:
                return True
        return False

    def _tem_vermelho(self, recorte_bgr: np.ndarray) -> bool:
        # Há vermelho suficiente? (o número do Resultado é vermelho).
        hsv = cv2.cvtColor(recorte_bgr, cv2.COLOR_BGR2HSV)
        m1 = cv2.inRange(hsv, (0, 80, 80), (12, 255, 255))
        m2 = cv2.inRange(hsv, (160, 80, 80), (179, 255, 255))
        return float(cv2.bitwise_or(m1, m2).mean()) / 255.0 > 0.02

    def _roi_e_vermelho(self, recorte: np.ndarray, x: int, y: int, w: int, h: int) -> bool:
        # Esta caixinha (x,y,largura,altura) é sobretudo vermelha?
        zona = recorte[y : y + h, x : x + w]
        if zona.size == 0:
            return False
        hsv = cv2.cvtColor(zona, cv2.COLOR_BGR2HSV)
        m1 = cv2.inRange(hsv, (0, 70, 70), (14, 255, 255))
        m2 = cv2.inRange(hsv, (160, 70, 70), (179, 255, 255))
        return float(cv2.bitwise_or(m1, m2).mean()) / 255.0 >= 0.12

    def _classificar_digito(self, crop_bgr: np.ndarray) -> tuple[str, float] | None:
        # Compara com fotos 0-9 e diz qual dígito parece + a confiança.
        if crop_bgr.size == 0 or min(crop_bgr.shape[:2]) < 8:
            return None
        alvo = _mascara_vermelha(crop_bgr)
        if int(alvo.sum()) < 40:
            return None

        melhor: tuple[str, float] | None = None
        for digito, silhueta in self.digitos_mask.items():
            escala = 0.90 * min(
                crop_bgr.shape[0] / max(silhueta.shape[0], 1),
                crop_bgr.shape[1] / max(silhueta.shape[1], 1),
            )
            wr = max(8, int(silhueta.shape[1] * escala))
            hr = max(8, int(silhueta.shape[0] * escala))
            if hr >= alvo.shape[0] or wr >= alvo.shape[1]:
                continue
            dig = cv2.resize(silhueta, (wr, hr), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(alvo, dig, cv2.TM_CCOEFF_NORMED)
            _, mv, _, _ = cv2.minMaxLoc(res)
            score = float(mv) if np.isfinite(mv) else -1.0
            if melhor is None or score > melhor[1]:
                melhor = (digito, score)
        if melhor is None or melhor[1] < 0.45:
            return None
        return melhor

    def _traco_horizontal_em(self, mask: np.ndarray, dig_h: int) -> bool:
        # True se a máscara tiver um traço vermelho largo e baixo (sinal menos −).
        if mask.size == 0:
            return False
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        mh, mw = mask.shape[:2]
        for i in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[i])
            if area < 18:
                continue
            if w / max(h, 1) < 1.25:
                continue
            if h > max(22, int(dig_h * 0.45)):
                continue
            if x + w / 2 > mw * 0.85:
                continue
            return True
        ys, xs = np.where(mask > 0)
        if len(xs) >= 15:
            bw = int(xs.max() - xs.min() + 1)
            bh = int(ys.max() - ys.min() + 1)
            if bw >= bh * 1.4 and bh <= max(18, int(dig_h * 0.4)):
                if float(mask.mean()) / 255.0 >= 0.08:
                    return True
        return False

    def _menos_colado_no_blob(
        # O menos às vezes cola-se ao dígito (−7 vira um blob só); tenta separar.
        self, recorte: np.ndarray, dig_x: int, dig_y: int, dig_w: int, dig_h: int
    ) -> tuple[bool, int]:
        """Detecta menos fundido a esquerda do digito. Devolve (tem_menos, corte_px).

        So dispara em blobs largos: prefixo de colunas baixas (traco) seguido
        de corpo alto (digito). Evita cortar o proprio digito (ex.: 7).
        """
        if dig_w < 18 or dig_h < 20:
            return False, 0
        # digito sozinho quase nunca passa de ~1.0; merge tipico ~1.15+
        if dig_w / max(dig_h, 1) < 1.0:
            return False, 0
        zona = recorte[dig_y : dig_y + dig_h, dig_x : dig_x + dig_w]
        mask = _mascara_vermelha(zona)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        # banda media: o menos fica a meia altura
        y0 = int(dig_h * 0.18)
        y1 = int(dig_h * 0.82)
        mid = mask[y0:y1, :]
        col_h: list[int] = []
        for xx in range(mid.shape[1]):
            ys = np.where(mid[:, xx] > 0)[0]
            col_h.append(0 if len(ys) == 0 else int(ys.max() - ys.min() + 1))
        if len(col_h) < 10:
            return False, 0
        lim_baixo = max(12, int(dig_h * 0.34))
        lim_alto = max(20, int(dig_h * 0.45))
        # prefixo de colunas baixas (traco) ate aparecer corpo do digito
        i = 0
        tinta_baixa = 0
        while i < len(col_h) and col_h[i] <= lim_baixo:
            if col_h[i] >= 3:
                tinta_baixa += 1
            i += 1
        if tinta_baixa < 5:
            return False, 0
        if i >= len(col_h) - 4:
            return False, 0
        if max(col_h[i:]) < lim_alto:
            return False, 0
        # confirma traco continuo na faixa cortada
        corte = max(i, int(dig_w * 0.18))
        faixa = mid[:, :corte]
        if not self._traco_horizontal_em(faixa, dig_h):
            # ainda assim, perfil baixo+alto e evidencia forte
            if tinta_baixa < 8:
                return False, 0
        return True, min(corte, dig_w - 8)

    def _tem_menos_esquerda(
        # Há um traço de menos à esquerda do número? → número negativo.
        self, recorte: np.ndarray, dig_x: int, dig_y: int, dig_w: int, dig_h: int
    ) -> bool:
        """Procura o traco vermelho a esquerda do digito (alinhado na vertical)."""
        rh, rw = recorte.shape[:2]
        x1 = max(0, dig_x + max(2, dig_w // 5))
        x0 = max(0, dig_x - max(12, int(dig_w * 1.4)))
        y0 = max(0, dig_y + int(dig_h * 0.20))
        y1 = min(rh, dig_y + int(dig_h * 0.80))
        if x1 - x0 < 6 or y1 - y0 < 6:
            return False
        zona = recorte[y0:y1, x0:x1]
        # 1) template do menos
        if _hits_template(zona, self.menos, self.menos_m, (0.6, 0.9, 1.2, 1.6, 2.2, 3.0), 0.45):
            return True
        # 2) blob / densidade horizontal nesta faixa
        mask = _mascara_vermelha(zona)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        zh, zw = zona.shape[:2]
        mid = mask[int(zh * 0.20) : int(zh * 0.80), : int(zw * 0.75)]
        return self._traco_horizontal_em(mid, dig_h)

    def _ler_digitos(self, recorte: np.ndarray) -> int | None:
        # Lê o número vermelho no círculo (ex.: 7 ou -7).
        # OPEN leve: tenta separar o menos do dígito quando estão colados.
        mask = _mascara_vermelha(recorte)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        n, _labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)

        blobs: list[tuple[int, int, int, int, int]] = []
        for i in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[i])
            if area < 40:
                continue
            # Ignora as letras vermelhas de "Resultado" no topo do círculo.
            if y < recorte.shape[0] * 0.30 and h < recorte.shape[0] * 0.20:
                continue
            blobs.append((x, y, w, h, area))

        if not blobs:
            return self._ler_digitos_template(recorte)

        # x_dig = origem do digito (apos cortar menos colado, se houver)
        digitos: list[tuple[int, str, float, int, int, int, int, bool]] = []
        for x, y, w, h, area in blobs:
            razao = w / max(h, 1)
            # candidatos a digito: relativamente altos
            if h < max(16, int(recorte.shape[0] * 0.12)):
                continue
            if razao >= 1.8 and h <= max(20, int(recorte.shape[0] * 0.28)):
                # traco horizontal isolado — nao e digito
                continue
            colado, corte = self._menos_colado_no_blob(recorte, x, y, w, h)
            x_dig, w_dig = x, w
            if colado and corte > 0:
                x_dig = x + corte
                w_dig = max(8, w - corte)
            elif razao > 1.05:
                # blob largo: corta esquerda para classificar; o menos
                # fica a esquerda de x_dig e _tem_menos_esquerda apanha-o
                corte = max(4, int(w * 0.32))
                x_dig = x + corte
                w_dig = max(8, w - corte)
            crop = recorte[
                max(0, y - 2) : min(recorte.shape[0], y + h + 2),
                max(0, x_dig - 2) : min(recorte.shape[1], x_dig + w_dig + 2),
            ]
            cls = self._classificar_digito(crop)
            if cls is None:
                continue
            digito, score = cls
            digitos.append((x_dig, digito, score, y, w_dig, h, area, colado))

        if not digitos:
            # #region agent log
            _dbg(
                "C",
                "hypatyamat.py:_ler_digitos",
                "sem digitos apos blobs",
                {"n_blobs": len(blobs)},
            )
            # #endregion
            return self._ler_digitos_template(recorte)

        digitos.sort(key=lambda t: t[2], reverse=True)
        escolhidos: list[tuple[int, str, float, int, int, int, int, bool]] = []
        for item in digitos:
            if any(abs(item[0] - e[0]) < 18 for e in escolhidos):
                continue
            escolhidos.append(item)
        escolhidos.sort(key=lambda t: t[0])
        escolhidos = escolhidos[:3]

        primeiro = escolhidos[0]
        fx, _fd, _fs, fy, fw, fh, _fa, colado = primeiro
        negativo = bool(colado) or self._tem_menos_esquerda(recorte, fx, fy, fw, fh)
        # reforco: blob horizontal isolado a esquerda do primeiro digito
        if not negativo:
            for x, y, w, h, area in blobs:
                if x + w > fx + 2:
                    continue
                # ignora ruido nas bordas do recorte
                if y < 4 or y + h > recorte.shape[0] - 4:
                    continue
                cy = y + h / 2
                if not (fy <= cy <= fy + fh):
                    continue
                if w / max(h, 1) >= 1.25 and h <= max(24, int(fh * 0.5)):
                    negativo = True
                    break

        texto = "".join(d for _, d, _, *_ in escolhidos)
        try:
            valor = int(texto)
        except ValueError:
            return None
        final = -valor if negativo else valor
        # #region agent log
        _dbg(
            "B",
            "hypatyamat.py:_ler_digitos",
            "resultado lido",
            {
                "negativo_flag": negativo,
                "colado": colado,
                "texto": texto,
                "valor_final": final,
                "escolhidos": [
                    {"x": int(x), "d": d, "sc": round(sc, 3), "colado": c}
                    for x, d, sc, *_rest, c in escolhidos
                ],
            },
        )
        # #endregion
        return final

    def _ler_digitos_template(self, recorte: np.ndarray) -> int | None:
        # Plano B: ler só por comparação com fotos de dígitos.
        hits: list[tuple[int, int, int, int, float, str]] = []
        for etiqueta, (modelo, mask) in self.digitos.items():
            for x, y, w, h, conf in _hits_template(
                recorte, modelo, mask, ESCALAS_DIGITOS, LIMIAR_DIGITO
            ):
                if y < recorte.shape[0] * 0.25:
                    continue
                if not self._roi_e_vermelho(recorte, x, y, w, h):
                    continue
                hits.append((x, y, w, h, conf, etiqueta[0]))
        if not hits:
            return None
        hits.sort(key=lambda t: t[4], reverse=True)
        escolhidos: list[tuple[int, int, int, int, float, str]] = []
        for hit in hits:
            x, y, w, h, conf, dig = hit
            if any(abs(x + w / 2 - (ex + ew / 2)) < max(w, ew) * 0.5 for ex, ey, ew, eh, _, _ in escolhidos):
                continue
            escolhidos.append(hit)
        escolhidos.sort(key=lambda t: t[0])
        escolhidos = escolhidos[:3]
        primeiro = escolhidos[0]
        px, py, pw, ph = primeiro[0], primeiro[1], primeiro[2], primeiro[3]
        colado, corte = self._menos_colado_no_blob(recorte, px, py, pw, ph)
        if colado and corte > 0:
            px, pw = px + corte, max(8, pw - corte)
        negativo = colado or self._tem_menos_esquerda(recorte, px, py, pw, ph)
        texto = "".join(d for *_, d in escolhidos)
        try:
            valor = int(texto)
        except ValueError:
            return None
        return -valor if negativo else valor

    def encontrar_caixa(self, tela: np.ndarray) -> tuple[int, int, int, int] | None:
        # Onde está o círculo Resultado? Devolve (x, y, largura, altura).
        for x, y, w, h, _conf in self._rois_circulo(tela):
            x0 = max(0, x - int(0.1 * w))
            y0 = max(0, y - int(0.1 * h))
            x1 = min(tela.shape[1], x + w + int(0.1 * w))
            y1 = min(tela.shape[0], y + h + int(0.2 * h))
            recorte = tela[y0:y1, x0:x1]
            if not self._tem_vermelho(recorte):
                continue
            if not self._tem_resultado_rapido(recorte) and not self._tem_resultado_ocr(recorte):
                continue
            return x0, y0, x1 - x0, y1 - y0
        return None

    def ler(self, frame_bgra: np.ndarray) -> int | None:
        # Passo a passo: achar círculo amarelo → confirmar "Resultado" → ler número.
        tela = np.ascontiguousarray(frame_bgra[:, :, :3])
        for x, y, w, h, _conf in self._rois_circulo(tela):
            x0 = max(0, x - int(0.1 * w))
            y0 = max(0, y - int(0.1 * h))
            x1 = min(tela.shape[1], x + w + int(0.1 * w))
            y1 = min(tela.shape[0], y + h + int(0.2 * h))
            recorte = tela[y0:y1, x0:x1]
            if not self._tem_vermelho(recorte):
                continue
            if not self._tem_resultado_rapido(recorte) and not self._tem_resultado_ocr(recorte):
                continue
            valor = self._ler_digitos(recorte)
            if valor is not None:
                return valor
        return None


# LEITOR DO TEMPO (relogio)
# So o valor 0 muda o jogo: nesse caso procura-se a fila de setas.
class LeitorTempo:
    """Encontra o icone do relogio, o circulo branco ao lado, e le o tempo."""

    def __init__(self) -> None:
        self.relogio_bgr, self.relogio_m = carregar_rgba(ASSETS / "relogio.png")
        self.digitos = self._digitos_pretos()

    @staticmethod
    def _digitos_pretos() -> dict[int, list[np.ndarray]]:
        temps: dict[int, list[np.ndarray]] = {d: [] for d in range(10)}
        for d in range(10):
            for escala, espessura in ((2.2, 5), (2.6, 6), (3.0, 7)):
                canvas = np.ones((110, 80), np.uint8) * 255
                cv2.putText(
                    canvas,
                    str(d),
                    (8, 85),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    escala,
                    0,
                    espessura,
                    cv2.LINE_AA,
                )
                tinta = cv2.threshold(canvas, 127, 255, cv2.THRESH_BINARY_INV)[1]
                n = _norm_sil(tinta, (28, 40))
                if n is not None:
                    temps[d].append(n)
        # templates reais do exemplo, se existirem
        for caminho, rois in (
            (
                ASSETS / "relogio_tempo_exemplo.png",
                ((98, 108, 74, 115, 2), (185, 108, 80, 116, 0)),
            ),
        ):
            if not caminho.exists():
                continue
            img = cv2.imread(str(caminho))
            if img is None:
                continue
            for x, y, w, h, dig in rois:
                if y + h > img.shape[0] or x + w > img.shape[1]:
                    continue
                crop = img[y : y + h, x : x + w]
                cinza = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                tinta = cv2.threshold(cinza, 100, 255, cv2.THRESH_BINARY_INV)[1]
                n = _norm_sil(tinta, (28, 40))
                if n is not None:
                    temps[dig].append(n)
        return temps

    def _tem_anel_azul(self, zona: np.ndarray) -> bool:
        if zona.size == 0:
            return False
        hsv = cv2.cvtColor(zona, cv2.COLOR_BGR2HSV)
        azul = cv2.inRange(hsv, (90, 70, 70), (135, 255, 255))
        return float(azul.mean()) / 255.0 >= 0.12

    def _circulos_tempo(self, tela: np.ndarray) -> list[tuple[int, int, int, int, float]]:
        """Candidatos a circulo branco do tempo (lado esquerdo, grandes)."""
        hsv = cv2.cvtColor(tela, cv2.COLOR_BGR2HSV)
        # branco / quase-branco (sat baixa; evita quadrados peach da grelha)
        branco = cv2.inRange(hsv, (0, 0, 155), (180, 70, 255))
        branco = cv2.morphologyEx(branco, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        contornos, _ = cv2.findContours(branco, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        rois: list[tuple[int, int, int, int, float]] = []
        min_area = max(2_500, int(tela.shape[0] * tela.shape[1] * 0.00035))
        for contorno in contornos:
            x, y, w, h = cv2.boundingRect(contorno)
            area = w * h
            if area < min_area or area > 350_000:
                continue
            if not (0.7 <= w / max(h, 1) <= 1.4):
                continue
            peri = cv2.arcLength(contorno, True)
            if peri <= 0:
                continue
            circ = 4 * np.pi * cv2.contourArea(contorno) / (peri * peri)
            if circ < 0.45:
                continue
            if x > tela.shape[1] * 0.55:
                continue
            recorte = tela[y : y + h, x : x + w]
            hsv_r = cv2.cvtColor(recorte, cv2.COLOR_BGR2HSV)
            # rejeitar peach/laranja da grelha (sat media/alta)
            if float(hsv_r[:, :, 1].mean()) > 75:
                continue
            # precisa de tinta escura no centro (o numero do tempo)
            cinza = cv2.cvtColor(recorte, cv2.COLOR_BGR2GRAY)
            tinta = cv2.threshold(cinza, 130, 255, cv2.THRESH_BINARY_INV)[1]
            mi = int(h * 0.18)
            centro = tinta[mi : h - mi, mi : w - mi]
            if float(centro.mean()) / 255.0 < 0.02:
                continue
            bonus = 0.25 if x < tela.shape[1] * 0.35 else 0.0
            if y < tela.shape[0] * 0.45:
                bonus += 0.1
            rois.append((x, y, w, h, circ + bonus + min(0.2, area / 100000)))
        rois.sort(key=lambda r: r[4], reverse=True)
        return rois[:6]

    def _icone_perto_do_circulo(
        self, tela: np.ndarray, circulo: tuple[int, int, int, int]
    ) -> bool:
        x, y, w, h = circulo
        x0 = max(0, x - int(0.35 * w))
        y0 = max(0, y - int(0.35 * h))
        x1 = min(tela.shape[1], x + int(0.55 * w))
        y1 = min(tela.shape[0], y + int(0.55 * h))
        zona = tela[y0:y1, x0:x1]
        if zona.size == 0:
            return False
        base, mask = self.relogio_bgr, self.relogio_m
        for escala in (0.6, 0.9, 1.2, 1.6, 2.0):
            wr = max(10, int(base.shape[1] * escala))
            hr = max(10, int(base.shape[0] * escala))
            if hr >= zona.shape[0] or wr >= zona.shape[1]:
                continue
            if wr > w * 0.7 or hr > h * 0.7:
                continue
            modelo = cv2.resize(base, (wr, hr), interpolation=cv2.INTER_AREA)
            mask_r = cv2.resize(mask, (wr, hr), interpolation=cv2.INTER_NEAREST)
            conf, (lx, ly) = _match_bgr(zona, modelo, mask_r)
            if conf < 0.82:
                continue
            patch = zona[ly : ly + hr, lx : lx + wr]
            if self._tem_anel_azul(patch):
                return True
        return False

    def _ler_digitos_pretos(self, recorte: np.ndarray) -> int | None:
        h, w = recorte.shape[:2]
        cinza = cv2.cvtColor(recorte, cv2.COLOR_BGR2GRAY)
        for limiar in (90, 110, 130, 150):
            tinta = cv2.threshold(cinza, limiar, 255, cv2.THRESH_BINARY_INV)[1]
            # ignora canto do icone do relogio e bordo
            tinta[: max(1, int(h * 0.16)), : max(1, int(w * 0.36))] = 0
            tinta[:6, :] = 0
            tinta[-6:, :] = 0
            tinta[:, :6] = 0
            tinta[:, -6:] = 0
            tinta = cv2.morphologyEx(tinta, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
            n, _, stats, _ = cv2.connectedComponentsWithStats(tinta, 8)
            blobs: list[tuple[int, int, int, int]] = []
            for i in range(1, n):
                x, y, bw, bh, area = (int(v) for v in stats[i])
                if area < max(40, int(h * w * 0.01)) or bh < h * 0.2:
                    continue
                if bw > w * 0.75:
                    continue
                blobs.append((x, y, bw, bh))
            if not blobs:
                continue
            blobs.sort(key=lambda b: b[0])
            blobs = blobs[:2]
            digitos: list[int] = []
            ok = True
            for x, y, bw, bh in blobs:
                crop = tinta[y : y + bh, x : x + bw]
                nrm = _norm_sil(crop, (28, 40))
                if nrm is None:
                    ok = False
                    break
                melhor: tuple[int, float] | None = None
                for d, lista in self.digitos.items():
                    for t in lista:
                        sc = _score_sil(nrm, t)
                        if melhor is None or sc > melhor[1]:
                            melhor = (d, sc)
                if melhor is None or melhor[1] < 0.50:
                    ok = False
                    break
                digitos.append(melhor[0])
            if not ok or not digitos:
                continue
            valor = int("".join(str(d) for d in digitos))
            if 0 <= valor <= 99:
                return valor
        return None

    def ler(self, frame_bgra: np.ndarray) -> int | None:
        # Só devolve o tempo se houver círculo branco com o relógio ao lado.
        tela = np.ascontiguousarray(frame_bgra[:, :, :3])
        candidatos: list[tuple[float, int, int, int, int]] = []

        for x, y, w, h, score in self._circulos_tempo(tela):
            if not self._icone_perto_do_circulo(tela, (x, y, w, h)):
                continue
            candidatos.append((score + 2.0, x, y, w, h))

        if not candidatos:
            return None

        candidatos.sort(key=lambda t: t[0], reverse=True)
        vistos: set[tuple[int, int, int, int]] = set()
        for _sc, x, y, w, h in candidatos:
            chave = (x // 8, y // 8, w // 8, h // 8)
            if chave in vistos:
                continue
            vistos.add(chave)
            recorte = tela[y : y + h, x : x + w]
            if recorte.size == 0 or min(recorte.shape[:2]) < 12:
                continue
            valor = self._ler_digitos_pretos(recorte)
            if valor is not None:
                return valor
        return None


# LEITOR DO SINAL (+ − × ÷)
# Olha logo por baixo do círculo "Resultado".
# Decide pela COR (e forma): rosa=×, azul=÷, vermelho=−, amarelo=+.
class LeitorSinal:
    # Descobre se o sinal por baixo do Resultado é + − × ou ÷.

    def __init__(self) -> None:
        # Carrega fotos de exemplo de cada sinal (+ - * /).
        self.templates: list[tuple[str, np.ndarray]] = []
        # Atenção: NÃO usar o botão vermelho com ÷ como foto do menos
        # (parece um −). Preferir ÷ azul + silhueta branca do ÷.
        mapa = {
            "+": ["mais_amarelo.png", "sinal_mais_exemplo.png"],
            "-": ["sinal_menos.png"],
            "*": ["sinal_multiplicacao.png"],
            "/": ["sinal_divisao.png"],
        }
        for op, nomes in mapa.items():
            for nome in nomes:
                caminho = ASSETS / nome
                if not caminho.exists():
                    continue
                bgr, _mask = carregar_rgba(caminho)
                self.templates.append((op, bgr))
        branco = ASSETS / "sinal_divisao_branco.png"
        if branco.exists():
            img = cv2.imread(str(branco), cv2.IMREAD_GRAYSCALE)
            self.div_branco = img if img is not None else None
        else:
            self.div_branco = None
        # ÷ com alpha — match mascarado funciona sobre agua azul
        caminho_div = ASSETS / "sinal_divisao.png"
        if caminho_div.exists():
            self.div_bgr, self.div_mask = carregar_rgba(caminho_div)
        else:
            self.div_bgr, self.div_mask = None, None

    @staticmethod
    def _zona_sinal(
        # Recorta a faixa logo por baixo do círculo Resultado (onde está o sinal).
        tela: np.ndarray, caixa_resultado: tuple[int, int, int, int] | None
    ) -> np.ndarray | None:
        if caixa_resultado is None:
            return None
        x, y, w, h = caixa_resultado
        # faixa curta sob o circulo — evita agua/baloes que geram falsos */÷
        x0 = max(0, x - int(0.05 * w))
        y0 = max(0, y + int(h * 0.90))
        x1 = min(tela.shape[1], x + w + int(0.12 * w))
        y1 = min(tela.shape[0], y + h + int(h * 0.55))
        zona = tela[y0:y1, x0:x1]
        return None if zona.size == 0 else zona

    @staticmethod
    def _fracao_hsv(rec: np.ndarray, low: tuple[int, int, int], high: tuple[int, int, int]) -> float:
        # Que percentagem da imagem está nesta cor (intervalo HSV)?
        if rec.size == 0:
            return 0.0
        hsv = cv2.cvtColor(rec, cv2.COLOR_BGR2HSV)
        return float(cv2.inRange(hsv, low, high).mean()) / 255.0

    @classmethod
    def _e_magenta(cls, rec: np.ndarray) -> bool:
        # Rosa/magenta forte → multiplicação (×).
        """× do jogo e rosa/magenta (H ~160-168)."""
        return cls._fracao_hsv(rec, (148, 60, 70), (170, 255, 255)) >= 0.12

    @classmethod
    def _e_azul_op(cls, rec: np.ndarray) -> bool:
        # Azul forte → divisão (÷). Atenção: água/fundo azul pode enganar.
        """÷ do jogo e azul (H ~102-114)."""
        return cls._fracao_hsv(rec, (95, 60, 70), (130, 255, 255)) >= 0.12

    @classmethod
    def _e_vermelho_menos(cls, rec: np.ndarray) -> bool:
        # Vermelho forte → subtração (−).
        """− e vermelho puro (H baixo ou muito alto), nao o rosa do ×."""
        low = cls._fracao_hsv(rec, (0, 70, 70), (14, 255, 255))
        hi = cls._fracao_hsv(rec, (170, 70, 70), (179, 255, 255))
        mag = cls._fracao_hsv(rec, (148, 60, 70), (170, 255, 255))
        return (low + hi) >= 0.12 and (low + hi) >= mag * 0.85

    @staticmethod
    def _tem_pontos_divisao(zona: np.ndarray, bar: tuple[int, int, int, int]) -> bool:
        # O ÷ tem um ponto acima e outro abaixo da barra; procura esses pontos.
        bx, by, bw, bh = bar
        cx = bx + bw // 2
        gap = max(2, int(bh * 0.35))
        max_dist = max(14, int(bh * 2.2), int(bw * 0.7))
        hsv = cv2.cvtColor(zona, cv2.COLOR_BGR2HSV)
        branco = cv2.inRange(hsv, (0, 0, 190), (180, 70, 255))
        azul = cv2.inRange(hsv, (95, 70, 70), (130, 255, 255))
        mask = cv2.bitwise_or(branco, azul)
        mask[max(0, by) : by + bh, max(0, bx) : bx + bw] = 0
        cima = False
        baixo = False
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        for i in range(1, n):
            x, y, ww, hh, area = (int(v) for v in stats[i])
            if area < 8 or area > 500:
                continue
            if max(ww, hh) > max(14, int(bw * 0.45)):
                continue
            razao = max(ww, hh) / max(1, min(ww, hh))
            if razao > 2.2:
                continue
            pcx = x + ww / 2
            if abs(pcx - cx) > bw * 0.55:
                continue
            if by - max_dist <= y + hh <= by - gap + 2:
                cima = True
            if by + bh + gap - 2 <= y <= by + bh + max_dist:
                baixo = True
        return cima and baixo

    @staticmethod
    def _e_amarelo(rec: np.ndarray) -> bool:
        # Amarelo → adição (+).
        if rec.size == 0:
            return False
        hsv = cv2.cvtColor(rec, cv2.COLOR_BGR2HSV)
        yel = cv2.inRange(hsv, (15, 100, 120), (40, 255, 255))
        return float(yel.mean()) / 255.0 >= 0.18

    def _match_divisao_mascara(self, zona: np.ndarray) -> float:
        # Compara a zona com a foto do ÷ (só a forma, ignorando fundo).
        """Score 0..1 do ÷ azul via template com alpha (robusto no fundo azul)."""
        if self.div_bgr is None or self.div_mask is None:
            return 0.0
        melhor = 0.0
        zh, zw = zona.shape[:2]
        for escala in (0.55, 0.7, 0.85, 1.0, 1.15, 1.35, 1.6, 2.0):
            wr = max(10, int(self.div_bgr.shape[1] * escala))
            hr = max(10, int(self.div_bgr.shape[0] * escala))
            if hr >= zh or wr >= zw:
                continue
            modelo = cv2.resize(self.div_bgr, (wr, hr), interpolation=cv2.INTER_AREA)
            mask = cv2.resize(self.div_mask, (wr, hr), interpolation=cv2.INTER_AREA)
            if int((mask > 20).sum()) < 40:
                continue
            res = cv2.matchTemplate(zona, modelo, cv2.TM_SQDIFF_NORMED, mask=mask)
            mn = float(res.min()) if res.size else 1.0
            score = 1.0 - mn
            if score > melhor:
                melhor = score
        return melhor

    def _divisao_por_brilho(self, zona: np.ndarray) -> bool:
        # Plano B: achar uma barra horizontal clara com pontos (forma do ÷).
        """÷ sobre agua: barra+pontos mais brilhantes que o fundo azul local."""
        hsv = cv2.cvtColor(zona, cv2.COLOR_BGR2HSV)
        h, s, v = cv2.split(hsv)
        med_v = cv2.GaussianBlur(v, (31, 31), 0)
        # azul-ciano localmente mais claro que a agua
        sel = (h >= 95) & (h <= 135) & (v.astype(np.int16) > med_v.astype(np.int16) + 8)
        feat = np.zeros(v.shape, np.uint8)
        feat[sel] = 255
        # highlights brancos do ÷
        feat[(s < 90) & (v > 210)] = 255
        feat = cv2.morphologyEx(feat, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        n, _, stats, _ = cv2.connectedComponentsWithStats(feat, 8)
        blobs = []
        for i in range(1, n):
            x, y, w, h, a = (int(v) for v in stats[i])
            if a < 25:
                continue
            blobs.append((x, y, w, h, a))
        if len(blobs) < 2:
            return False
        # procura barra horizontal + ponto acima e abaixo
        for bx, by, bw, bh, ba in blobs:
            razao = bw / max(bh, 1)
            if razao < 1.8 or bh > max(22, int(zona.shape[0] * 0.28)):
                continue
            cx = bx + bw / 2
            cima = baixo = False
            for x, y, w, h, a in blobs:
                if (x, y, w, h) == (bx, by, bw, bh):
                    continue
                if max(w, h) > max(22, int(bw * 0.55)):
                    continue
                if abs((x + w / 2) - cx) > bw * 0.55:
                    continue
                cy = y + h / 2
                if cy < by - 2:
                    cima = True
                if cy > by + bh + 2:
                    baixo = True
            if cima and baixo:
                return True
        return False

    def _heuristica(self, zona: np.ndarray) -> str | None:
        # Decide o sinal pela COR primeiro (rosa=*, azul=/, vermelho=-, amarelo=+).
        hsv = cv2.cvtColor(zona, cv2.COLOR_BGR2HSV)
        # × rosa/magenta — separado do vermelho do menos
        magenta = cv2.inRange(hsv, (148, 60, 70), (170, 255, 255))
        red_lo = cv2.inRange(hsv, (0, 70, 70), (14, 255, 255))
        red_hi = cv2.inRange(hsv, (170, 70, 70), (179, 255, 255))
        red = cv2.bitwise_or(red_lo, red_hi)
        yel = cv2.inRange(hsv, (18, 120, 150), (40, 255, 255))
        azul = cv2.inRange(hsv, (95, 70, 80), (130, 255, 255))
        area_zona = zona.shape[0] * zona.shape[1]

        def blobs(mask: np.ndarray, min_a: int = 50) -> list[tuple[int, int, int, int, int]]:
            n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
            out = []
            for i in range(1, n):
                x, y, w, h, a = (int(v) for v in stats[i])
                if a >= min_a:
                    out.append((x, y, w, h, a))
            out.sort(key=lambda t: t[4], reverse=True)
            return out

        # 1) Multiplicação = rosa/magenta (NUNCA confundir com azul da água)
        for x, y, w, h, a in blobs(magenta, 80)[:4]:
            if a > area_zona * 0.35:
                continue
            razao = w / max(h, 1)
            if 0.55 <= razao <= 1.45 and a >= 120 and h >= 18:
                rec = zona[y : y + h, x : x + w]
                if self._e_magenta(rec):
                    return "*"

        # 2) Adição = amarelo
        for x, y, w, h, a in blobs(yel, 120)[:3]:
            razao = w / max(h, 1)
            if y < zona.shape[0] * 0.08:
                continue
            if 0.75 <= razao <= 1.35 and a >= 150 and h >= 22:
                rec = zona[y : y + h, x : x + w]
                if self._e_amarelo(rec):
                    return "+"

        # 3) Subtração = barra vermelha horizontal (não rosa do ×, sem pontos de ÷)
        for x, y, w, h, a in blobs(red, 70)[:6]:
            razao = w / max(h, 1)
            if razao >= 1.35 and h <= max(40, int(zona.shape[0] * 0.45)):
                if self._tem_pontos_divisao(zona, (x, y, w, h)):
                    continue
                rec = zona[y : y + h, x : x + w]
                if self._e_magenta(rec):
                    continue
                if self._e_vermelho_menos(rec):
                    return "-"

        # 4) divisao: pontos classicos, OU brilho local (÷ na agua), OU template mascarado
        for x, y, w, h, a in blobs(azul, 60)[:8]:
            if a > area_zona * 0.18 or w > zona.shape[1] * 0.65:
                continue
            if a < 80:
                continue
            razao = w / max(h, 1)
            rec = zona[y : y + h, x : x + w]
            if not self._e_azul_op(rec):
                continue
            if 0.45 <= razao <= 2.2 and h >= 14:
                if self._tem_pontos_divisao(zona, (x, y, w, h)):
                    return "/"
        # nunca ÷ se ha × rosa na zona
        if not self._e_magenta(zona):
            if self._divisao_por_brilho(zona):
                return "/"
            if self._match_divisao_mascara(zona) >= 0.94:
                return "/"
        return None

    def _melhor_template(self, zona: np.ndarray) -> str | None:
        # Plano B: comparar a zona com as fotos de cada sinal e escolher a melhor.
        """Templates com gate de cor: * so magenta, / so azul+pontos, + so amarelo."""
        melhores: dict[str, float] = {}
        for op, modelo in self.templates:
            limiar = {"+": 0.58, "*": 0.52, "/": 0.52, "-": 0.55}.get(op, 0.55)
            for escala in (0.5, 0.8, 1.1, 1.5, 2.0, 2.5, 3.0):
                wr = max(8, int(modelo.shape[1] * escala))
                hr = max(8, int(modelo.shape[0] * escala))
                if hr >= zona.shape[0] or wr >= zona.shape[1]:
                    continue
                m = cv2.resize(modelo, (wr, hr), interpolation=cv2.INTER_AREA)
                conf, (lx, ly) = _match_bgr(zona, m, None)
                if conf < limiar:
                    continue
                rec = zona[ly : ly + hr, lx : lx + wr]
                if op == "+" and not self._e_amarelo(rec):
                    continue
                if op == "*" and not self._e_magenta(rec):
                    continue
                if op == "/":
                    if not self._e_azul_op(rec):
                        continue
                    # exige pontos do ÷, salvo match muito forte do template azul
                    if conf < 0.68 and not self._tem_pontos_divisao(zona, (lx, ly, wr, hr)):
                        continue
                if op == "-" and (self._e_magenta(rec) or self._e_azul_op(rec)):
                    continue
                if conf > melhores.get(op, -1.0):
                    melhores[op] = conf
        if not melhores:
            return None
        # se ha rosa na zona, * ganha a / (fundo azul nao conta)
        if "*" in melhores and "/" in melhores and self._e_magenta(zona):
            return "*"
        return max(melhores.items(), key=lambda t: t[1])[0]

    def ler(self, tela: np.ndarray, caixa_resultado: tuple[int, int, int, int] | None) -> str | None:
        # Devolve '+', '-', '*', '/' ou None se não conseguir ler o sinal.
        zona = self._zona_sinal(tela, caixa_resultado)
        if zona is None:
            return None

        # rosa × tem prioridade absoluta sobre qualquer azul do fundo
        if self._e_magenta(zona):
            hsv = cv2.cvtColor(zona, cv2.COLOR_BGR2HSV)
            mag = cv2.inRange(hsv, (148, 60, 70), (170, 255, 255))
            if float(mag.mean()) / 255.0 >= 0.015:
                h = self._heuristica(zona)
                if h == "*":
                    # #region agent log
                    _dbg("E", "hypatyamat.py:LeitorSinal.ler", "sinal magenta", {"sinal": "*"})
                    # #endregion
                    return "*"

        h = self._heuristica(zona)
        if h is not None:
            # #region agent log
            _dbg(
                "E",
                "hypatyamat.py:LeitorSinal.ler",
                "sinal heuristica",
                {
                    "sinal": h,
                    "div_mask": round(self._match_divisao_mascara(zona), 3),
                    "magenta": self._e_magenta(zona),
                },
            )
            # #endregion
            return h

        t = self._melhor_template(zona)
        # #region agent log
        _dbg("E", "hypatyamat.py:LeitorSinal.ler", "sinal template", {"sinal": t})
        # #endregion
        return t


# LEITOR DA GRELHA DE QUADRADOS
# Encontra cada quadrado peach, lê o dígito, vê se tem traço de negativo,
# e se está selecionado (moldura vermelha à volta).
class LeitorGrelha:
    # Encontra os quadrados peach, lê o número de cada um e se está selecionado.
    # Ignora estrelinhas; um traço à esquerda torna o número negativo.

    def __init__(self) -> None:
        # Carrega fotos de exemplo dos dígitos da grelha (pasta grelha_tpl).
        self.temps: dict[int, list[np.ndarray]] = {d: [] for d in range(10)}
        if GRELHA_TPL.exists():
            for caminho in GRELHA_TPL.glob("*.png"):
                try:
                    digito = int(caminho.stem.split("_")[0])
                except ValueError:
                    continue
                img = cv2.imread(str(caminho), cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    self.temps[digito].append(img)
        for d in range(10):
            for caminho in ASSETS.glob(f"numero_{d}*.png"):
                if "resultado" in caminho.name:
                    continue
                im = cv2.imread(str(caminho), cv2.IMREAD_UNCHANGED)
                if im is None:
                    continue
                if im.ndim == 3 and im.shape[2] == 4:
                    bgr, mask = im[:, :, :3], im[:, :, 3]
                else:
                    bgr = im[:, :, :3] if im.ndim == 3 else cv2.cvtColor(im, cv2.COLOR_GRAY2BGR)
                    mask = np.full(bgr.shape[:2], 255, np.uint8)
                hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
                sat = cv2.inRange(hsv, (0, 25, 25), (179, 255, 255))
                sat[mask < 20] = 0
                sat = cv2.morphologyEx(sat, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
                n = _norm_sil(sat)
                if n is not None:
                    self.temps[d].append(n)
        # template extra do 4 verde da grelha (falhava com frequência)
        amostra_4 = ASSETS / "_dbg_cells" / "07_327_137.png"
        if amostra_4.exists():
            img4 = cv2.imread(str(amostra_4))
            if img4 is not None:
                _neg, sil4 = self._silhueta(img4)
                n4 = _norm_sil(sil4)
                if n4 is not None:
                    self.temps[4].append(n4)

    def _celulas(
        # Encontra cada quadrado peach na zona da grelha (posição e tamanho).
        self,
        tela: np.ndarray,
        caixa_resultado: tuple[int, int, int, int] | None = None,
    ) -> list[tuple[int, int, int, int]]:
        hsv = cv2.cvtColor(tela, cv2.COLOR_BGR2HSV)
        peach = cv2.inRange(hsv, (8, 30, 150), (35, 180, 255))
        peach = cv2.morphologyEx(peach, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        contornos, _ = cv2.findContours(peach, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        # limites: por defeito largos; se houver Resultado, ancora a grelha a esquerda dele
        if caixa_resultado is not None:
            rx, ry, rw, rh = caixa_resultado
            x_min = max(0, rx - int(rw * 8.5))
            x_max = min(tela.shape[1], rx + int(rw * 0.35))
            y_min = max(0, ry - int(rh * 0.4))
            y_max = min(tela.shape[0], ry + int(rh * 4.5))
        else:
            x_min = int(tela.shape[1] * 0.05)
            x_max = int(tela.shape[1] * 0.95)
            y_min = int(tela.shape[0] * 0.03)
            y_max = int(tela.shape[0] * 0.95)
        cells: list[tuple[int, int, int, int]] = []
        for contorno in contornos:
            x, y, w, h = cv2.boundingRect(contorno)
            area = w * h
            if area < 1800 or area > 16000:
                continue
            if not (0.65 <= w / max(h, 1) <= 1.45):
                continue
            cx, cy = x + w / 2, y + h / 2
            if cx < x_min or cx > x_max or cy < y_min or cy > y_max:
                continue
            cells.append((x, y, w, h))
        cells.sort(key=lambda t: t[2] * t[3], reverse=True)
        kept: list[tuple[int, int, int, int]] = []
        for c in cells:
            cx, cy = c[0] + c[2] / 2, c[1] + c[3] / 2
            if any(
                abs(cx - (k[0] + k[2] / 2)) < 30 and abs(cy - (k[1] + k[3] / 2)) < 30
                for k in kept
            ):
                continue
            kept.append(c)
        kept.sort(key=lambda t: (t[1] // 28, t[0]))
        return kept

    def _silhueta(self, cell: np.ndarray) -> tuple[bool, np.ndarray]:
        # Extrai a forma do dígito no quadrado; o bool diz se há menos (negativo).
        # Cópia: vamos apagar estrelas amarelas dos cantos sem mexer no ecrã.
        cell = np.ascontiguousarray(cell).copy()
        h, w = cell.shape[:2]
        if min(h, w) >= 24:
            hsv0 = cv2.cvtColor(cell, cv2.COLOR_BGR2HSV)
            yel = cv2.inRange(hsv0, (12, 90, 150), (42, 255, 255))
            mrg = int(min(h, w) * 0.30)
            canto = np.zeros(yel.shape, np.uint8)
            canto[:mrg, :mrg] = 255
            canto[:mrg, w - mrg :] = 255
            canto[h - mrg :, :mrg] = 255
            canto[h - mrg :, w - mrg :] = 255
            yel = cv2.bitwise_and(yel, canto)
            if int(yel.sum()) > 0:
                borda = np.concatenate(
                    [
                        cell[1:5, 1 : w - 1].reshape(-1, 3),
                        cell[h - 5 : h - 1, 1 : w - 1].reshape(-1, 3),
                    ],
                    0,
                )
                cell[yel > 0] = np.median(borda, axis=0)
        hsv = cv2.cvtColor(cell, cv2.COLOR_BGR2HSV)
        border = np.concatenate(
            [
                cell[1:6, 1 : w - 1].reshape(-1, 3),
                cell[h - 6 : h - 1, 1 : w - 1].reshape(-1, 3),
                cell[1 : h - 1, 1:6].reshape(-1, 3),
                cell[1 : h - 1, w - 6 : w - 1].reshape(-1, 3),
            ],
            0,
        ).astype(np.float32)
        ref = np.median(border, axis=0)
        diff = np.linalg.norm(cell.astype(np.float32) - ref, axis=2)
        sat = hsv[:, :, 1].astype(np.float32)
        val = hsv[:, :, 2].astype(np.float32)
        med_sat = float(np.median(sat[1:6, 1 : w - 1]))
        dig = ((diff > 38) | (sat > med_sat + 28) | (val < 130)).astype(np.uint8) * 255

        # Traço vermelho à esquerda = número negativo.
        # Regras estritas: um 9 vermelho (ou brilhos) NÃO deve virar -9.
        m1 = cv2.inRange(hsv, (0, 70, 70), (14, 255, 255))
        m2 = cv2.inRange(hsv, (155, 70, 70), (179, 255, 255))
        red = cv2.bitwise_or(m1, m2)
        neg = False
        faixa = red[:, : int(w * 0.50)]
        n, _, stats, _ = cv2.connectedComponentsWithStats(faixa, 8)
        for i in range(1, n):
            x, y, ww, hh, area = (int(v) for v in stats[i])
            # menos real tipico ~16x10 e area ~120; ruido/digito fica fora
            if area < 45 or ww < 10:
                continue
            razao = ww / max(hh, 1)
            if razao < 1.55 or razao > 4.5:
                continue
            if hh > max(12, int(h * 0.24)):
                continue
            cy = y + hh / 2.0
            # a meia altura (nao estrela no topo nem base)
            if cy < h * 0.30 or cy > h * 0.70:
                continue
            # centrado na metade esquerda — fora do corpo do digito
            if x + ww / 2.0 > w * 0.40:
                continue
            neg = True
            dig[max(0, y - 1) : y + hh + 1, max(0, x - 1) : x + ww + 1] = 0
            break

        # Apaga estrelinhas decorativas no canto — sem comer o dígito 4.
        dig[: max(1, int(h * 0.16)), : max(1, int(w * 0.26))] = 0
        # CLOSE pequeno (3×3): um 5×5 tapava o buraco do 9 e ele parecia um 2.
        dig = cv2.morphologyEx(dig, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        dig = cv2.morphologyEx(dig, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

        n, labels, stats, _ = cv2.connectedComponentsWithStats(dig, 8)
        cands = [
            (int(stats[i, 4]), i)
            for i in range(1, n)
            if stats[i, 4] >= 30 and stats[i, 3] >= h * 0.2
        ]
        cands.sort(reverse=True)
        keep = np.zeros_like(dig)
        if not cands:
            return neg, keep
        main = cands[0][1]
        keep[labels == main] = 255
        x0, y0, ww0, hh0, _ = (int(v) for v in stats[main])
        cx0, cy0 = x0 + ww0 / 2, y0 + hh0 / 2
        for _, i in cands[1:3]:
            x, y, ww, hh, _ = (int(v) for v in stats[i])
            if abs((x + ww / 2) - cx0) < w * 0.4 and abs((y + hh / 2) - cy0) < h * 0.45:
                keep[labels == i] = 255
        return neg, keep

    def _classificar(self, sil: np.ndarray) -> tuple[int, float] | None:
        # Compara a silhueta com fotos 0-9 e devolve (dígito, confiança).
        n = _norm_sil(sil)
        if n is None:
            return None
        scores: dict[int, float] = {}
        for digito, lista in self.temps.items():
            for t in lista:
                if t.shape != n.shape:
                    t = cv2.resize(t, (n.shape[1], n.shape[0]), interpolation=cv2.INTER_AREA)
                sc = _score_sil(n, t)
                if digito not in scores or sc > scores[digito]:
                    scores[digito] = sc
        if not scores:
            return None
        melhor_d = max(scores, key=scores.get)
        melhor_sc = scores[melhor_d]
        if melhor_sc < LIMIAR_DIGITO_GRELHA:
            return None

        # O brilho do dígito tapa os buracos, por isso a contagem mente:
        # um 8 verdadeiro ficava com "1 buraco" e era forçado a 0.
        # A forma (template) decide. Os buracos só desempatam se as notas estão coladas.
        n_buracos = _contar_buracos(sil)
        s2 = scores.get(2, -1.0)
        s9 = scores.get(9, -1.0)
        s0 = scores.get(0, -1.0)
        s8 = scores.get(8, -1.0)

        if melhor_d in (2, 9) and s2 >= 0.0 and s9 >= 0.0 and abs(s9 - s2) <= 0.04:
            if n_buracos >= 1:
                return 9, max(s9, melhor_sc)
            return 2, max(s2, melhor_sc)

        if melhor_d in (0, 8) and s0 >= 0.0 and s8 >= 0.0 and abs(s0 - s8) <= 0.04:
            # empate: 2 buracos → 8; 1 buraco e o 0 não perde → 0; senão 8
            if n_buracos >= 2 or s8 > s0:
                return 8, max(s8, melhor_sc)
            return 0, max(s0, melhor_sc)

        return melhor_d, melhor_sc

    @staticmethod
    def _esta_selecionado(cell: np.ndarray) -> bool:
        # Tem moldura vermelha à volta? → já está selecionado (um clique desmarca).
        h, w = cell.shape[:2]
        if min(h, w) < 24:
            return False
        hsv = cv2.cvtColor(cell, cv2.COLOR_BGR2HSV)
        m1 = cv2.inRange(hsv, (0, 120, 160), (16, 255, 255))
        m2 = cv2.inRange(hsv, (168, 120, 160), (179, 255, 255))
        red = cv2.bitwise_or(m1, m2)
        m = max(2, int(min(h, w) * 0.04))
        bw = max(3, int(min(h, w) * 0.14))
        strips = (
            red[m : m + bw, m : w - m],
            red[h - m - bw : h - m, m : w - m],
            red[m : h - m, m : m + bw],
            red[m : h - m, w - m - bw : w - m],
        )
        lados = sum(1 for s in strips if s.size and float(s.mean()) / 255.0 >= 0.18)
        return lados >= 3

    def ler(
        self,
        frame_bgra: np.ndarray,
        caixa_resultado: tuple[int, int, int, int] | None = None,
    ) -> list[tuple[int, int, int, bool]]:
        # Devolve lista de quadrados: (valor, centro_x, centro_y, selecionado?).
        # Ex.: (-2, 400, 500, False) = número -2 no ecrã, ainda não selecionado.
        tela = np.ascontiguousarray(frame_bgra[:, :, :3])
        out: list[tuple[int, int, int, bool, float]] = []
        for x, y, w, h in self._celulas(tela, caixa_resultado):
            cell = tela[y : y + h, x : x + w]
            neg, sil = self._silhueta(cell)
            cls = self._classificar(sil)
            if cls is None:
                continue
            digito, sc = cls
            valor = -digito if neg else digito
            # A moldura vermelha fica muitas vezes FORA do peach. Sem esta margem
            # o bot não via a seleção, clicava outra vez e desmarcava.
            pad = max(5, int(min(w, h) * 0.14))
            x0 = max(0, x - pad)
            y0 = max(0, y - pad)
            x1 = min(tela.shape[1], x + w + pad)
            y1 = min(tela.shape[0], y + h + pad)
            sel = self._esta_selecionado(tela[y0:y1, x0:x1])
            out.append((valor, x + w // 2, y + h // 2, sel, sc))
        # Se o mesmo sítio foi lido várias vezes, fica só o mais confiante.
        out.sort(key=lambda t: t[4], reverse=True)
        escolhidos: list[tuple[int, int, int, bool]] = []
        for valor, cx, cy, sel, _sc in out:
            if any(abs(cx - ex) < 25 and abs(cy - ey) < 25 for _, ex, ey, _ in escolhidos):
                continue
            escolhidos.append((valor, cx, cy, sel))
        return escolhidos


# ESCOLHER A JOGADA E CLICAR COM CUIDADO
# - Cada QUADRADO (posição no ecrã) só pode ser usado uma vez por jogada
# - Quadrados com moldura vermelha já estão selecionados
# - Se estiverem selecionados à toa, desmarcamos antes
# - Só clicamos nos dois certos se ainda não estiverem selecionados
def _mesmo_quadrado(a: tuple, c: tuple, tol: int = 20) -> bool:
    # True se a e c são o MESMO quadrado no ecrã (posição), não só o mesmo número.
    # Ex.: dois "4" em sítios diferentes → False; o mesmo sítio lido duas vezes → True.
    return abs(int(a[1]) - int(c[1])) < tol and abs(int(a[2]) - int(c[2])) < tol


def _chave_quadrado(n: tuple) -> tuple[int, int]:
    # Identidade grosseira do quadrado pela posição (não pelo valor escrito).
    return (int(n[1]) // 15, int(n[2]) // 15)


def resolver_jogada(
    # Procura dois QUADRADOS DIFERENTES cuja conta (a ⊕ c) dê exatamente o Resultado.
    numeros: list[tuple],
    op: str,
    resultado: int,
) -> tuple[tuple, tuple] | None:
    # Cada item de 'numeros' é (valor, x, y, selecionado?).
    # Experimenta todos os PARES ordenados (a, c): a é o PRIMEIRO clique, c o segundo.
    # Em 8−3=5 o par tem de ser (8, 3), não (3, 8).
    # Em 0÷6=0 o par tem de ser (0, 6), NUNCA (6, 0).
    melhor = None
    melhor_dist = 1e18  # preferimos o par cujos centros estão mais perto um do outro
    for i, a in enumerate(numeros):
        for j, c in enumerate(numeros):
            if i == j or _mesmo_quadrado(a, c):
                continue  # nunca o mesmo sítio duas vezes
            # ÷ por zero é erro — nunca candidatar (n, 0)
            if op == "/" and c[0] == 0:
                continue
            # Resultado 0 com ÷: o 0 TEM de ser o primeiro (dividendo)
            if op == "/" and resultado == 0 and a[0] != 0:
                continue
            val = aplicar_op(a[0], op, c[0])  # conta na ordem a ⊕ c
            if val is None or val != resultado:
                continue  # esta ordem não dá o Resultado (ex.: 3−8≠5)
            dist = (a[1] - c[1]) ** 2 + (a[2] - c[2]) ** 2
            if dist < melhor_dist:
                melhor_dist = dist
                melhor = (a, c)
    if melhor is not None:
        return melhor

    # Plano B (só + com Resultado negativo):
    # se o OCR esqueceu os menos, tenta |a|+|c| == |resultado|
    # Ex.: Resultado -6, casas lidas como 2 e 4 em vez de -2 e -4.
    if op == "+" and resultado < 0:
        alvo = -resultado
        melhor_dist = 1e18
        for i, a in enumerate(numeros):
            for j, c in enumerate(numeros):
                if i == j or _mesmo_quadrado(a, c) or a[0] == 0 or c[0] == 0:
                    continue
                if abs(a[0]) + abs(c[0]) != alvo:
                    continue
                dist = (a[1] - c[1]) ** 2 + (a[2] - c[2]) ** 2
                bonus = (0 if a[0] < 0 else 50_000) + (0 if c[0] < 0 else 50_000)
                score = bonus + dist
                if score < melhor_dist:
                    melhor_dist = score
                    melhor = (a, c)
        if melhor is not None:
            return melhor

    # Plano B (multiplicação): |a|*|c| == |resultado|.
    # Ex.: Resultado -12 e ×, grelha tem -6 e 2 — se o OCR leu 6 em vez de -6,
    # a conta exacta falha (6*2=12), mas os absolutos batem certo → ainda assim joga.
    # Preferimos pares cujo sinal OCR já está certo; senão aceitamos na mesma
    # (o jogo usa o número real do quadrado, não o que o OCR pensou).
    if op == "*":
        alvo = abs(resultado)
        preciso = 1 if resultado >= 0 else -1
        melhor_dist = 1e18
        for i, a in enumerate(numeros):
            for j, c in enumerate(numeros):
                if i == j or _mesmo_quadrado(a, c) or a[0] == 0 or c[0] == 0:
                    continue
                if abs(a[0]) * abs(c[0]) != alvo:
                    continue
                prod_sinais = (1 if a[0] > 0 else -1) * (1 if c[0] > 0 else -1)
                dist = (a[1] - c[1]) ** 2 + (a[2] - c[2]) ** 2
                bonus = 0 if prod_sinais == preciso else 50_000
                score = bonus + dist
                if score < melhor_dist:
                    melhor_dist = score
                    melhor = (a, c)
        if melhor is not None:
            return melhor

    # Plano B (divisão): mesma ideia — a ÷ c = resultado, com absolutos.
    if op == "/" and resultado != 0:
        melhor_dist = 1e18
        for i, a in enumerate(numeros):
            for j, c in enumerate(numeros):
                if i == j or _mesmo_quadrado(a, c) or c[0] == 0:
                    continue
                if abs(a[0]) % abs(c[0]) != 0:
                    continue  # só divisões exatas (sem resto)
                if abs(a[0]) // abs(c[0]) != abs(resultado):
                    continue
                dist = (a[1] - c[1]) ** 2 + (a[2] - c[2]) ** 2
                if dist < melhor_dist:
                    melhor_dist = dist
                    melhor = (a, c)
        if melhor is not None:
            return melhor

    # Divisão com Resultado 0 (plano B): só 0 ÷ c (c ≠ 0), 0 primeiro.
    if op == "/" and resultado == 0:
        melhor_dist = 1e18
        melhor = None
        for i, a in enumerate(numeros):
            if a[0] != 0:
                continue
            for j, c in enumerate(numeros):
                if i == j or _mesmo_quadrado(a, c) or c[0] == 0:
                    continue
                dist = (a[1] - c[1]) ** 2 + (a[2] - c[2]) ** 2
                if dist < melhor_dist:
                    melhor_dist = dist
                    melhor = (a, c)
        if melhor is not None:
            return melhor

    return None


def fundir_grelha(
    # Junta a leitura deste instante com as recentes (cache curta) para não 'perder' digitos.
    atuais: list[tuple[int, int, int, bool]],
    cache: dict[tuple[int, int], tuple[int, int, int, bool, float]],
    agora: float,
    ttl: float = 0.7,
) -> list[tuple[int, int, int, bool]]:
    # Assim, se um quadrado falhar num frame, ainda o lembramos ~2 segundos.
    for valor, cx, cy, sel in atuais:
        chave = (int(cx) // 18, int(cy) // 18)
        cache[chave] = (valor, int(cx), int(cy), bool(sel), agora)
    expirados = [k for k, (*_, t) in cache.items() if agora - t > ttl]
    for k in expirados:
        del cache[k]
    return [(v, x, y, sel) for (v, x, y, sel, _t) in cache.values()]


def _grelha_parece_nova(
    antes: list[tuple],
    depois: list[tuple],
) -> bool:
    # True se a grelha mudou de verdade (jogada certa costuma alterar vários quadrados).
    if abs(len(antes) - len(depois)) >= 2:
        return True
    mudou = 0
    for va, xa, ya, *_ in antes:
        achou = False
        for vd, xd, yd, *_ in depois:
            if abs(int(xa) - int(xd)) < 28 and abs(int(ya) - int(yd)) < 28:
                achou = True
                if int(va) != int(vd):
                    mudou += 1
                break
        if not achou:
            mudou += 1
    return mudou >= 2


def tentar_todas_combinacoes(
    ox: int,
    oy: int,
    numeros: list[tuple],
    op: str,
    resultado_antes: int,
    pausado: threading.Event | None,
    ler_estado,
    atualizar_progresso,
    memoria: dict | None = None,
) -> bool:
    # Só quando restam EXACTAMENTE 2 quadrados e a conta lida não fechou.
    # Esses dois SÃO o par: experimenta a ordem (e a inversa em − e ÷).
    # "Acertar" = o Resultado mudou ou a grelha renovou-se.
    if len(numeros) != 2:
        return False

    pares: list[tuple[tuple, tuple]] = []
    for i, a in enumerate(numeros):
        for j, c in enumerate(numeros):
            if i == j or _mesmo_quadrado(a, c):
                continue
            # + e × : a ordem não importa → só uma direção por par de sítios
            if op in ("+", "*") and i > j:
                continue
            # ÷ : nunca propor n÷0. Resultado 0 → o 0 tem de ir primeiro.
            if op == "/" and int(c[0]) == 0:
                continue
            if op == "/" and resultado_antes == 0 and int(a[0]) != 0:
                continue
            pares.append((a, c))

    for k, (a, c) in enumerate(pares):
        if pausado is not None and pausado.is_set():
            return False
        try:
            atualizar_progresso(
                f"teste {k + 1}/{len(pares)}: {a[0]}{'×' if op == '*' else op}{c[0]}"
            )
        except Exception:
            pass

        # Sempre limpa seleção e clica a → c (forcar_ordem), para a ordem ser a da tentativa
        executar_par(
            ox,
            oy,
            numeros,
            a,
            c,
            op=op,
            pausado=pausado,
            forcar_ordem=True,
            memoria=memoria,
        )
        if pausado is not None and pausado.is_set():
            return False

        time.sleep(0.28)
        try:
            r_novo, nums_novos = ler_estado()
        except Exception:
            continue

        if r_novo is not None and int(r_novo) != int(resultado_antes):
            return True
        if _grelha_parece_nova(numeros, nums_novos):
            return True

        # Falhou: desmarca o que NÓS deixámos selecionado (a visão muitas vezes não vê a moldura).
        a_desmarcar = list(nums_novos) if nums_novos else list(numeros)
        for n in a_desmarcar:
            if pausado is not None and pausado.is_set():
                return False
            k = _chave_quadrado(n)
            visto = bool(memoria.get(k)) if memoria is not None else False
            if not visto and len(n) > 3 and bool(n[3]):
                visto = True
            if not visto:
                continue
            mover_rato(ox + int(n[1]), oy + int(n[2]))
            clicar_esquerdo()
            if memoria is not None:
                memoria[k] = False
        time.sleep(0.06)

    return False


def executar_par(
    # Clica com cuidado: desmarca errados (moldura vermelha) e seleciona os dois certos.
    ox: int,
    oy: int,
    numeros: list[tuple],
    a: tuple,
    c: tuple,
    op: str = "+",
    pausado: threading.Event | None = None,
    forcar_ordem: bool = False,
    memoria: dict | None = None,
) -> int:
    # Devolve quantos cliques fez (0 se os alvos já estavam selecionados e a ordem não importa).
    # `memoria` lembra o que NÓS clicámos. A moldura vermelha falha muitas vezes;
    # sem esta memória o bot clicava outra vez e desmarcava o quadrado.
    if _mesmo_quadrado(a, c):
        return 0
    mem = memoria if memoria is not None else {}

    def _parado() -> bool:
        return pausado is not None and pausado.is_set()

    def _clicar_em(px: int, py: int) -> bool:
        # False = pausaram a meio → abortar o resto dos cliques
        if _parado():
            return False
        mover_rato(ox + int(px), oy + int(py))
        clicar_esquerdo()
        return not _parado()

    def _marcado(n: tuple) -> bool:
        k = _chave_quadrado(n)
        if k in mem:
            return bool(mem[k])
        return bool(n[3]) if len(n) > 3 else False

    def _alternar(n: tuple) -> bool:
        # Um clique inverte a seleção. A memória acompanha esse inverso.
        estado = _marcado(n)
        if not _clicar_em(n[1], n[2]):
            return False
        mem[_chave_quadrado(n)] = not estado
        return True

    # Em − e ÷ a ORDEM dos cliques é a conta: primeiro a, depois c.
    # Ex.: Resultado 5 → tem de ser 8 depois 3 (8−3), NUNCA 3 depois 8.
    # forcar_ordem: usado no modo "testar as duas ordens" do par final.
    ordem_importa = forcar_ordem or op in ("-", "/")
    ka, kc = _chave_quadrado(a), _chave_quadrado(c)
    cliques = 0

    if ordem_importa:
        # Limpa TODA a seleção (mesmo os alvos). Crítico em 0÷n:
        # se o n já estava marcado e só clicássemos o 0, o jogo fazia n÷0.
        for n in numeros:
            if not _marcado(n):
                continue
            if not _alternar(n):
                return cliques
            cliques += 1
        time.sleep(0.08)
        # Clica sempre na ordem da conta: a → c  (ex.: 0 depois 6, NUNCA 6 depois 0)
        for alvo in (a, c):
            if not _alternar(alvo):
                return cliques
            cliques += 1
            time.sleep(0.05)
        return cliques

    # + e × : a ordem não muda o resultado; só desmarca o que não é a nem c
    for n in numeros:
        if not _marcado(n):
            continue
        kn = _chave_quadrado(n)
        if kn in (ka, kc):
            continue
        if not _alternar(n):
            return cliques
        cliques += 1
    # Clica cada alvo só se ainda NÃO estiver selecionado
    # (se já estiver, outro clique desmarcava — e isso bugava)
    for alvo in (a, c):
        if _marcado(alvo):
            continue  # já está marcado → não tocar
        if not _alternar(alvo):
            return cliques
        cliques += 1
    return cliques


# PROGRAMA PRINCIPAL
# 1) Pede admin  2) Abre a janelinha  3) Liga a visão do ecrã noutro fio
# 4) Em ciclo: ler Resultado + sinal + grelha → escolher par → clicar
def main() -> None:
    # Arranque: admin → janela → visão do ecrã → loop de jogo.
    pedir_admin()  # precisa de admin para o rato funcionar por cima do jogo

    # Carrega a foto do balão e cria os "leitores" (Resultado, sinal, grelha).
    template_bgr, mascara = carregar_rgba(TEMPLATE_BALAO)
    fila_setas = cv2.imread(str(ASSETS / "fila_setas.png"))
    if fila_setas is None:
        raise FileNotFoundError(f"Nao encontrei a foto da fila: {ASSETS / 'fila_setas.png'}")
    botao_jogar = cv2.imread(str(TEMPLATE_JOGAR))
    if botao_jogar is None:
        raise FileNotFoundError(f"Nao encontrei o botao Jogar: {TEMPLATE_JOGAR}")
    leitor = LeitorResultado()
    leitor_tempo = LeitorTempo()
    leitor_sinal = LeitorSinal()
    leitor_grelha = LeitorGrelha()
    visao = VisaoEcra()
    visao.ver()  # primeira fotografia (aquece a câmara)

    # Janelinha preta no canto: mostra o que o bot está a ver.
    janela = tk.Tk()
    janela.title("Hypatyamat")
    janela.configure(bg="black")
    janela.geometry(f"320x260+190+635")
    janela.attributes("-topmost", True)  # fica sempre por cima
    esconder_cmd()  # esconde a janela preta da consola

    texto_estado = tk.StringVar(value="Tempo: —\nResultado: —\nSinal: —\nJogada: —")
    tempo_ui: list[int | None] = [None]
    rotulo = tk.Label(
        janela,
        textvariable=texto_estado,
        bg="black",
        fg="white",
        font=("Segoe UI", 13, "bold"),
        justify="left",
    )
    rotulo.pack(expand=True, padx=12, pady=12)

    parar = threading.Event()
    pausado = threading.Event()  # set = bot parado (F6)
    pedido_reinicio = threading.Event()  # limpar cache / remapear após F6

    def atualizar_ui(
        # Atualiza o texto da janelinha preta com o que estamos a ver agora.
        resultado: int | None,
        sinal: str | None,
        jogada: str,
    ) -> None:
        if pausado.is_set():
            return  # não tapar a mensagem PAUSADO
        r = "—" if resultado is None else str(resultado)
        s = "—" if sinal is None else sinal
        t = "—" if tempo_ui[0] is None else str(tempo_ui[0])
        texto_estado.set(f"Tempo: {t}\nResultado: {r}\nSinal: {s}\nJogada: {jogada}")

    def ao_mudar_pausa(esta_pausado: bool) -> None:
        # Chamado pelo fio do F6 (pode ser noutro thread) → atualiza a janela.
        msg = "PAUSADO" if esta_pausado else "Tempo: —\nResultado: —\nSinal: —\nJogada: —"
        janela.after(0, lambda m=msg: texto_estado.set(m))

    def ciclo_visao() -> None:
        # LOOP PRINCIPAL (corre noutro fio, para a janela não congelar):
        # fotografar → ler Resultado/sinal/grelha → achar par → clicar → repetir
        periodo = 1.0 / FPS
        ultimo_dbg = 0.0
        ultimo_dbg_resultado: int | None = object()  # type: ignore[assignment]
        # Contador de cliques de resposta (não balões). Depois de jogar → remapear.
        cliques_resposta = 0
        remapear = False  # se True, neste ciclo só lê o ecrã (não clica)
        # Memória curta dos quadrados vistos para não perder um dígito num frame.
        cache_grelha: dict[tuple[int, int], tuple[int, int, int, bool, float]] = {}
        ultimo_alvo: tuple[int | None, str | None] = (None, None)
        sem_par_desde = 0.0
        # O que NÓS marcámos (a moldura vermelha nem sempre se vê).
        memoria_sel: dict[tuple[int, int], bool] = {}
        # Sinal estável: duas leituras iguais seguidas. Evita +/÷ a piscar.
        historico_sinal: list[str] = []
        sinal_estavel: str | None = None
        resultado_sinal: int | None = None
        # Par que acabámos de clicar. Não voltar a clicar (desmarcava) enquanto o jogo não muda.
        par_clique: tuple | None = None
        t_clique = 0.0
        res_clique: int | None = None
        falhados: set[tuple] = set()
        t_teste_falhou = 0.0
        # Tempo a 0: procura a fila das setas e clica. Outro valor não muda a jogada.
        t_zero: float | None = None
        t_ultimo_zero = 0.0
        setas_clicadas = False
        t_leitura_tempo = 0.0
        # «Jogar»: conta 3s desde que o botão aparece e só depois clica.
        t_viu_jogar: float | None = None
        while not parar.is_set():  # corre até fechares a janela
            inicio = time.perf_counter()

            # --- TRAVA DE SEGURANÇA (F6) — o fio _vigilante_f6 trata da tecla ---
            if pedido_reinicio.is_set():
                pedido_reinicio.clear()
                cache_grelha.clear()
                memoria_sel.clear()
                historico_sinal.clear()
                sinal_estavel = None
                par_clique = None
                falhados.clear()
                cliques_resposta = 0
                if not pausado.is_set():
                    remapear = True

            if pausado.is_set():
                resto = periodo - (time.perf_counter() - inicio)
                if resto > 0:
                    parar.wait(resto)
                continue

            try:
                frame = visao.ver()  # fotografia nova do ecrã
                tela = np.ascontiguousarray(frame[:, :, :3])  # só as cores (sem alpha)
                ox, oy = visao.origem  # canto do monitor (para converter coords → ecrã)
                agora = time.perf_counter()

                # Botão «Jogar»: espera 3s depois de aparecer e só então clica.
                # Enquanto ele estiver no ecrã não se joga a grelha por baixo.
                jogar = encontrar_botao_jogar(tela, botao_jogar)
                if jogar is not None:
                    if t_viu_jogar is None:
                        t_viu_jogar = agora
                    falta = 3.0 - (agora - t_viu_jogar)
                    if falta <= 0 and not pausado.is_set():
                        jx, jy = jogar
                        try:
                            mover_rato(ox + jx, oy + jy)
                            clicar_esquerdo()
                            t_viu_jogar = agora  # se continuar visível, espera outros 3s
                        except Exception:
                            pass
                        jogada_jogar = "Jogar"
                    else:
                        jogada_jogar = f"Jogar em {max(1, int(falta + 0.999))}s"
                    janela.after(0, atualizar_ui, None, None, jogada_jogar)
                    resto = periodo - (time.perf_counter() - inicio)
                    if resto > 0:
                        parar.wait(resto)
                    continue
                t_viu_jogar = None

                # O relógio fica no canto. Ler o número no ecrã inteiro, em todos
                # os frames, atrasava a jogada. O 0 vermelho vê-se sempre; o
                # número só se relê de vez em quando, para a janela.
                viu_zero = tempo_e_zero(
                    tela, leitor_tempo.relogio_bgr, leitor_tempo.relogio_m
                )
                if agora - t_leitura_tempo >= 0.45:
                    t_leitura_tempo = agora
                    canto = tela[: int(tela.shape[0] * 0.7), : int(tela.shape[1] * 0.6)]
                    tempo_lido = leitor_tempo.ler(canto)
                    if tempo_lido is not None:
                        tempo_ui[0] = tempo_lido
                        viu_zero = tempo_lido == 0
                if viu_zero:
                    t_ultimo_zero = agora
                    if t_zero is None:
                        t_zero = agora
                        setas_clicadas = False
                    tempo_ui[0] = 0
                elif t_zero is not None and agora - t_ultimo_zero > 0.6:
                    t_zero = None
                    setas_clicadas = False
                em_zero = t_zero is not None and (
                    viu_zero or agora - t_ultimo_zero <= 0.6
                )
                if em_zero:
                    if not setas_clicadas and not pausado.is_set():
                        setas = encontrar_fila_setas(tela, fila_setas)
                        if setas is not None:
                            sx, sy = setas
                            try:
                                mover_rato(ox + sx, oy + sy)
                                clicar_esquerdo()
                                setas_clicadas = True
                            except Exception:
                                pass
                    janela.after(
                        0,
                        atualizar_ui,
                        0,
                        None,
                        "setas" if setas_clicadas else "à procura das setas",
                    )
                    resto = periodo - (time.perf_counter() - inicio)
                    if resto > 0:
                        parar.wait(resto)
                    continue

                # --- 1) LER O QUE ESTÁ NO ECRÃ ---
                resultado = leitor.ler(frame)  # número do círculo amarelo
                caixa = leitor.encontrar_caixa(tela)  # onde está esse círculo
                sinal_lido = leitor_sinal.ler(tela, caixa)  # + - * / por baixo
                numeros_agora = leitor_grelha.ler(frame, caixa)  # quadrados

                # O sinal às vezes pisca (÷ num frame, × no seguinte). Só jogamos
                # quando a mesma leitura aparece duas vezes seguidas.
                if resultado != resultado_sinal:
                    historico_sinal.clear()
                    sinal_estavel = None
                    resultado_sinal = resultado
                if sinal_lido is not None:
                    historico_sinal.append(sinal_lido)
                    del historico_sinal[:-4]
                    if len(historico_sinal) >= 2 and historico_sinal[-1] == historico_sinal[-2]:
                        sinal_estavel = historico_sinal[-1]
                sinal = sinal_estavel

                if resultado is not None and sinal is not None:
                    alvo = (resultado, sinal)
                    if ultimo_alvo not in ((None, None), alvo):
                        cache_grelha.clear()
                        memoria_sel.clear()
                        falhados.clear()
                        par_clique = None
                        sem_par_desde = agora
                    ultimo_alvo = alvo

                # Junta a leitura deste instante com a cache curta.
                # Se o frame veio vazio, não jogamos posições velhas (fantasmas).
                # Se só há 2 quadrados agora, são esses — a cache não inventa mais.
                numeros_merged = fundir_grelha(numeros_agora, cache_grelha, agora)
                if not numeros_agora:
                    numeros = []
                elif len(numeros_agora) == 2 or len(numeros_merged) >= len(numeros_agora) + 2:
                    numeros = list(numeros_agora)
                else:
                    numeros = numeros_merged

                # A visão da moldura falha: o que já clicámos continua marcado.
                vistos_mem: set[tuple[int, int]] = set()
                numeros_mem: list[tuple] = []
                for n in numeros:
                    k = _chave_quadrado(n)
                    vistos_mem.add(k)
                    if len(n) > 3 and n[3]:
                        memoria_sel[k] = True
                    numeros_mem.append((n[0], n[1], n[2], bool(memoria_sel.get(k, False))))
                for k in list(memoria_sel):
                    if k not in vistos_mem:
                        del memoria_sel[k]
                numeros = numeros_mem

                # O par clicado não mudou o Resultado: não clicar outra vez (desmarcava).
                # Passado 1s, desiste dessa ordem, desmarca, e pode tentar a outra.
                a_espera = False
                if par_clique is not None and resultado is not None and resultado == res_clique:
                    if agora - t_clique < 1.0:
                        a_espera = True
                    else:
                        falhados.add(par_clique)
                        desmarcados: list[tuple] = []
                        for n in numeros:
                            k = _chave_quadrado(n)
                            if k in par_clique and memoria_sel.get(k):
                                mover_rato(ox + int(n[1]), oy + int(n[2]))
                                clicar_esquerdo()
                                memoria_sel[k] = False
                                desmarcados.append((n[0], n[1], n[2], False))
                            else:
                                desmarcados.append(n)
                        numeros = desmarcados
                        time.sleep(0.08)
                        par_clique = None
                elif (
                    par_clique is not None
                    and resultado is not None
                    and resultado != res_clique
                ):
                    falhados.clear()
                    par_clique = None
                    memoria_sel.clear()

                jogada_txt = "—"
                par = None
                testar_combinacoes = False  # só no par final (exactamente 2)
                n_sel = sum(1 for n in numeros if n[3])
                # --- 2) ESCOLHER DOIS QUADRADOS DIFERENTES CUJA CONTA DÁ O RESULTADO ---
                if resultado is not None and sinal is not None and not a_espera:
                    if numeros:
                        par = resolver_jogada(numeros, sinal, resultado)
                    if par is not None:
                        a, c = par
                        chave_par = (_chave_quadrado(a), _chave_quadrado(c))
                        if chave_par in falhados:
                            # Esta ordem já falhou. Com 2 quadrados, experimenta a inversa.
                            if len(numeros) == 2 and sinal in ("-", "/"):
                                inv = (_chave_quadrado(c), _chave_quadrado(a))
                                if inv not in falhados and not _mesmo_quadrado(a, c):
                                    par = (c, a)
                                    a, c = par
                                    chave_par = inv
                                else:
                                    par = None
                            else:
                                par = None
                    if par is not None:
                        a, c = par
                        jogada_txt = f"{a[0]} {sinal} {c[0]} = {resultado}"
                        sem_par_desde = agora
                    else:
                        jogada_txt = f"sem par ({len(numeros)} nº)"
                        # #region agent log
                        vals = sorted({n[0] for n in numeros})
                        _dbg(
                            "G",
                            "hypatyamat.py:ciclo_visao",
                            "sem par valores",
                            {
                                "resultado": resultado,
                                "sinal": sinal,
                                "valores": vals,
                                "n": len(numeros),
                                "n_sel": n_sel,
                            },
                        )
                        # #endregion
                        sem_par_desde = agora
                        if len(numeros) == 2:
                            # Par final: estes dois são a jogada, mesmo se a leitura falhou.
                            # Se as duas ordens já falharam há pouco, não fica a clicar em loop.
                            ch0 = _chave_quadrado(numeros[0])
                            ch1 = _chave_quadrado(numeros[1])
                            ordens = {(ch0, ch1), (ch1, ch0)}
                            if ordens <= falhados and agora - t_teste_falhou < 3.0:
                                jogada_txt = "sem par (2 nº)"
                            else:
                                falhados -= ordens
                                testar_combinacoes = True
                                jogada_txt = "a testar (2 nº)"
                        else:
                            # Não há teste aleatório com 3 ou mais quadrados.
                            cache_grelha.clear()
                            remapear = True
                elif a_espera:
                    jogada_txt = "à espera"

                # #region agent log
                if (resultado != ultimo_dbg_resultado) or (agora - ultimo_dbg >= 0.8):
                    _dbg(
                        "D",
                        "hypatyamat.py:ciclo_visao",
                        "estado jogada",
                        {
                            "resultado": resultado,
                            "resultado_negativo": resultado is not None and resultado < 0,
                            "sinal": sinal,
                            "n_numeros": len(numeros),
                            "n_agora": len(numeros_agora),
                            "n_sel": n_sel,
                            "tem_par": par is not None,
                            "jogada": jogada_txt,
                            "caixa": list(caixa) if caixa else None,
                            "cliques_resposta": cliques_resposta,
                            "remapear": remapear,
                        },
                    )
                    ultimo_dbg = agora
                    ultimo_dbg_resultado = resultado
                # #endregion

                janela.after(0, atualizar_ui, resultado, sinal, jogada_txt)

                if remapear:
                    remapear = False
                    resto = periodo - (time.perf_counter() - inicio)
                    if resto > 0:
                        parar.wait(resto)
                    continue

                jogou = False
                # --- 3) CLICAR COM CUIDADO (desmarcar errados; não repetir quadrado) ---
                if par is not None:
                    a, c = par
                    try:
                        if pausado.is_set():
                            continue
                        nums_click = numeros
                        # ÷: ordem é crítica (0÷n). Relê a moldura vermelha agora
                        # e garante que o 0 é mesmo o PRIMEIRO clique.
                        if sinal == "/":
                            fr = visao.ver()
                            te = np.ascontiguousarray(fr[:, :, :3])
                            nums_click = leitor_grelha.ler(
                                fr, leitor.encontrar_caixa(te)
                            ) or numeros

                            def _atualizar(alvo: tuple) -> tuple:
                                for n in nums_click:
                                    if _mesmo_quadrado(alvo, n):
                                        return n
                                return alvo

                            a, c = _atualizar(a), _atualizar(c)
                            if resultado == 0:
                                # 0 tem de ser o primeiro. Se a releitura não
                                # confirma 0÷n, não se escolhe um vizinho ao calhas:
                                # isso carregava num 0 a sério e o jogo dava erro.
                                if int(a[0]) != 0 and int(c[0]) == 0:
                                    a, c = c, a
                                if int(a[0]) != 0 or int(c[0]) == 0:
                                    raise RuntimeError("÷0 sem par seguro")
                                jogada_txt = f"{a[0]} / {c[0]} = {resultado}"
                                janela.after(
                                    0, atualizar_ui, resultado, sinal, jogada_txt
                                )
                            elif int(c[0]) == 0:
                                raise RuntimeError("÷0 sem par seguro")
                        feitos = executar_par(
                            ox,
                            oy,
                            nums_click,
                            a,
                            c,
                            sinal,
                            pausado=pausado,
                            memoria=memoria_sel,
                        )
                        jogou = feitos >= 0 and not _mesmo_quadrado(a, c)
                        if jogou:
                            chave_feita = (_chave_quadrado(a), _chave_quadrado(c))
                            if feitos > 0 or par_clique != chave_feita:
                                par_clique = chave_feita
                                t_clique = time.perf_counter()
                                res_clique = resultado
                            cliques_resposta += feitos
                    except Exception as erro_click:
                        # #region agent log
                        _dbg(
                            "F",
                            "hypatyamat.py:ciclo_visao",
                            "falha click",
                            {"erro": str(erro_click), "a": list(a)[:4], "c": list(c)[:4]},
                        )
                        # #endregion
                        jogou = False
                        if "÷0" in str(erro_click):
                            falhados.add((_chave_quadrado(a), _chave_quadrado(c)))
                    # Depois de tentar um par → volta a ler o ecrã (não spammar o mesmo).
                    if jogou:
                        cliques_resposta = 0
                        remapear = True

                # --- 3b) PAR FINAL (exactamente 2) e a conta lida não fechou ---
                elif testar_combinacoes and resultado is not None and sinal is not None:
                    if pausado.is_set():
                        continue

                    def _ler_estado_teste():
                        fr = visao.ver()
                        te = np.ascontiguousarray(fr[:, :, :3])
                        cx = leitor.encontrar_caixa(te)
                        return leitor.ler(fr), leitor_grelha.ler(fr, cx)

                    def _prog_teste(txt: str) -> None:
                        janela.after(0, atualizar_ui, resultado, sinal, txt)

                    try:
                        ok = tentar_todas_combinacoes(
                            ox,
                            oy,
                            numeros,
                            sinal,
                            resultado,
                            pausado,
                            _ler_estado_teste,
                            _prog_teste,
                            memoria_sel,
                        )
                        jogou = ok
                        if not ok and len(numeros) == 2:
                            ch0 = _chave_quadrado(numeros[0])
                            ch1 = _chave_quadrado(numeros[1])
                            falhados.add((ch0, ch1))
                            falhados.add((ch1, ch0))
                            t_teste_falhou = time.perf_counter()
                        jogada_txt = "par por teste" if ok else f"sem par ({len(numeros)} nº)"
                        janela.after(0, atualizar_ui, resultado, sinal, jogada_txt)
                    except Exception as erro_teste:
                        _dbg(
                            "F",
                            "hypatyamat.py:ciclo_visao",
                            "falha teste combinacoes",
                            {"erro": str(erro_teste)},
                        )
                        jogou = False
                    cliques_resposta = 0
                    cache_grelha.clear()
                    remapear = True

                # --- 4) SE NÃO HOUVE JOGADA, TENTA UM BALÃO VERDE ---
                if (
                    not jogou
                    and not pausado.is_set()
                    and not testar_combinacoes
                    and not a_espera
                    and not (resultado is not None and sinal is None)
                ):
                    hit = encontrar_balao(frame, template_bgr, mascara)
                    if hit is not None and not pausado.is_set():
                        cx, cy, _conf = hit
                        try:
                            mover_rato(ox + cx, oy + cy)
                            clicar_esquerdo()  # estoura o balão
                        except Exception:
                            pass
            except Exception as erro:
                # #region agent log
                _dbg("F", "hypatyamat.py:ciclo_visao", "excecao ciclo", {"erro": str(erro)})
                # #endregion
            resto = periodo - (time.perf_counter() - inicio)
            if resto > 0:
                parar.wait(resto)

    # Fio só do F6 (pausa fiável) + fio da visão do jogo.
    fio_f6 = threading.Thread(
        target=_vigilante_f6,
        name="trava-f6",
        args=(parar, pausado, pedido_reinicio, ao_mudar_pausa),
        daemon=True,
    )
    fio_f6.start()
    trabalhador = threading.Thread(target=ciclo_visao, name="visao-jogo", daemon=True)
    trabalhador.start()

    def ao_fechar() -> None:
        # Quando fechas a janela: pára o loop, espera o fio da visão, liberta a câmara.
        parar.set()
        fio_f6.join(timeout=0.5)
        trabalhador.join(timeout=1.5)
        visao.fechar()
        janela.destroy()

    janela.protocol("WM_DELETE_WINDOW", ao_fechar)  # X da janela chama ao_fechar
    janela.mainloop()  # fica à espera de eventos da janela (bloqueia aqui)


if __name__ == "__main__":
    # Só corre o programa se abrires este ficheiro diretamente (não se o importares).
    main()
