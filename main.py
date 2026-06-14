"""
Mouse Finder - 마우스를 흔들면 맥OS 스타일 커서 확대 애니메이션
트레이 아이콘 우클릭 → 설정 / 종료
"""
import tkinter as tk
from tkinter import ttk
import threading
import time
import ctypes
import ctypes.wintypes
import math
import sys
import json
import os
import winreg
from collections import deque
from pynput import mouse as pmouse

# ── 경로 ──────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

DEFAULT_CONFIG = {
    'sensitivity': 30,
    'autostart': False,
}

# ── Win32 상수 ─────────────────────────────────────────────
GWL_EXSTYLE       = -20
WS_EX_LAYERED     = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
LWA_COLORKEY      = 0x00000001
VREFRESH          = 116   # GetDeviceCaps index

APP_NAME = 'MouseFinder'
RUN_KEY  = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Run'

# 투명 배경 (마젠타 — 커서/글로우 색과 충돌 없음)
TRANSPARENT_BG    = '#fe00fe'
TRANSPARENT_BGREF = 0x00fe00fe   # Win32 COLORREF (BGR)

# ── 오버레이 창 크기 ────────────────────────────────────────
OVERLAY_SIZE = 520
HALF         = OVERLAY_SIZE // 2

# ── 커서 폴리곤 (tip=0,0 정규화) ──────────────────────────
CURSOR_NORM = [
    (0.00, 0.00),
    (0.00, 0.80),
    (0.22, 0.60),
    (0.37, 0.97),
    (0.50, 0.90),
    (0.34, 0.54),
    (0.60, 0.54),
]

CURSOR_BASE_PX = 28
CURSOR_MAX_SCL = 5.0
ANIM_DURATION  = 1.0   # 초


# ══════════════════════════════════════════════════════════
#  유틸
# ══════════════════════════════════════════════════════════
def get_cursor_pos() -> tuple[int, int]:
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def get_refresh_rate() -> int:
    """주 모니터의 수직 주사율(Hz) 반환. 실패 시 60."""
    hdc = ctypes.windll.user32.GetDC(0)
    hz  = ctypes.windll.gdi32.GetDeviceCaps(hdc, VREFRESH)
    ctypes.windll.user32.ReleaseDC(0, hdc)
    return hz if hz > 0 else 60


def apply_click_through(hwnd: int) -> None:
    """오버레이가 마우스 클릭을 아래 창으로 투과시키도록 설정."""
    style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, TRANSPARENT_BGREF, 0, LWA_COLORKEY)


# ── easing ────────────────────────────────────────────────
def ease_out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3


def ease_in_out_quad(t: float) -> float:
    return 2 * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 2 / 2


# ══════════════════════════════════════════════════════════
#  설정
# ══════════════════════════════════════════════════════════
def load_config() -> dict:
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════════════════
#  자동실행 레지스트리
# ══════════════════════════════════════════════════════════
def _exe_path() -> str:
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    return f'pythonw "{os.path.abspath(__file__)}"'


def set_autostart(enable: bool) -> None:
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    if enable:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _exe_path())
    else:
        try:
            winreg.DeleteValue(key, APP_NAME)
        except OSError:
            pass
    winreg.CloseKey(key)


def get_autostart() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


# ══════════════════════════════════════════════════════════
#  흔들기 감지기
# ══════════════════════════════════════════════════════════
class ShakeDetector:
    def __init__(self, on_shake, config: dict):
        self.on_shake = on_shake
        self.config   = config
        self._pts: deque = deque()
        self._lock       = threading.Lock()
        self._last_shake = 0.0

    @property
    def _speed_thresh(self) -> float:
        # s=10 → 3700 px/s (거의 안 발동), s=50 → 2500, s=100 → 1200
        s = max(10, min(100, self.config.get('sensitivity', 30)))
        return 3800 - s * 26

    @property
    def _reversal_min(self) -> int:
        # s=10 → 4회, s=50 → 3회, s=100 → 2회
        s = self.config.get('sensitivity', 30)
        return max(2, 5 - int(s / 35))

    def feed(self, x: int, y: int) -> None:
        now = time.perf_counter()
        with self._lock:
            self._pts.append((now, x, y))
            cutoff = now - 0.4
            while self._pts and self._pts[0][0] < cutoff:
                self._pts.popleft()
            pts = list(self._pts)

        if len(pts) < 4:
            return

        total   = sum(math.hypot(pts[i][1]-pts[i-1][1], pts[i][2]-pts[i-1][2])
                      for i in range(1, len(pts)))
        elapsed = pts[-1][0] - pts[0][0]
        if elapsed < 0.05:
            return

        speed = total / elapsed

        # 유효한 방향 전환만 카운트: 각 구간이 MIN_SEG_PX 이상 이동해야 인정
        MIN_SEG_PX = 40
        reversals  = 0
        cur_dir    = 0
        cur_dist   = 0.0
        for i in range(1, len(pts)):
            dx = pts[i][1] - pts[i-1][1]
            if dx == 0:
                continue
            direction = 1 if dx > 0 else -1
            if direction != cur_dir:
                if cur_dir != 0 and cur_dist >= MIN_SEG_PX:
                    reversals += 1
                cur_dir  = direction
                cur_dist = abs(dx)
            else:
                cur_dist += abs(dx)

        if (speed >= self._speed_thresh
                and reversals >= self._reversal_min
                and now - self._last_shake > 0.8):
            self._last_shake = now
            self.on_shake()

    def start(self) -> None:
        listener = pmouse.Listener(on_move=lambda x, y: self.feed(x, y))
        listener.daemon = True
        listener.start()


# ══════════════════════════════════════════════════════════
#  오버레이 창
# ══════════════════════════════════════════════════════════
class CursorOverlay:
    """
    맥OS 스타일 커서 확대 애니메이션:
      1) 빠르게 커짐 (ease-out, 0 → 30% 구간)
      2) 최대 크기에서 잠깐 머묾 (30~45%)
      3) 부드럽게 원래 크기로 복귀 (ease-in-out, 45~100%)
    + 스포트라이트 글로우 + 방사형 링
    """

    def __init__(self):
        hz = get_refresh_rate()
        self._fps_ms = max(8, round(1000 / hz))   # 주사율 기반 프레임 간격

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.attributes('-transparentcolor', TRANSPARENT_BG)
        self.root.configure(bg=TRANSPARENT_BG)
        self.root.geometry(f'{OVERLAY_SIZE}x{OVERLAY_SIZE}+0+0')

        self.canvas = tk.Canvas(
            self.root, bg=TRANSPARENT_BG,
            highlightthickness=0,
            width=OVERLAY_SIZE, height=OVERLAY_SIZE,
        )
        self.canvas.pack()
        self._animating = False

        # 창이 실제로 그려진 뒤 즉시 click-through 적용
        self.root.update()
        apply_click_through(self.root.winfo_id())

    # ── 공개 API ─────────────────────────────────────────
    def trigger(self) -> None:
        self.root.after(0, self._start)

    def run(self) -> None:
        self.root.mainloop()

    # ── 내부 ─────────────────────────────────────────────
    def _start(self) -> None:
        self._animating = True
        mx, my = get_cursor_pos()
        self.root.geometry(f'{OVERLAY_SIZE}x{OVERLAY_SIZE}+{mx-HALF}+{my-HALF}')
        self.root.deiconify()
        self.root.lift()
        # 표시 직후 다시 한번 click-through 보장
        apply_click_through(self.root.winfo_id())

        handles: list[int] = []
        t0 = time.perf_counter()
        self._frame(t0, handles)

    def _frame(self, t0: float, handles: list) -> None:
        # 커서 위치 추적 → 창 이동
        mx, my = get_cursor_pos()
        self.root.geometry(f'{OVERLAY_SIZE}x{OVERLAY_SIZE}+{mx-HALF}+{my-HALF}')

        for h in handles:
            self.canvas.delete(h)
        handles.clear()

        if not self._animating:
            self.root.withdraw()
            return

        elapsed = time.perf_counter() - t0
        prog    = min(elapsed / ANIM_DURATION, 1.0)

        if prog >= 1.0:
            self.root.withdraw()
            self._animating = False
            return

        scale = self._mac_scale(prog)
        self._draw(handles, HALF, HALF, scale, prog)
        self.root.after(self._fps_ms, lambda: self._frame(t0, handles))

    @staticmethod
    def _mac_scale(prog: float) -> float:
        """
        맥OS 스타일 커서 크기 커브
          0.00~0.30 : ease-out 빠른 확대
          0.30~0.45 : 최대 크기 유지 (살짝 흔들리는 느낌)
          0.45~1.00 : ease-in-out 부드러운 복귀
        """
        if prog < 0.30:
            t = prog / 0.30
            e = ease_out_cubic(t)
            return 1.0 + e * (CURSOR_MAX_SCL - 1.0)
        elif prog < 0.45:
            # 최대 + 약간 진동 (±3%)
            wobble = math.sin((prog - 0.30) / 0.15 * math.pi * 2) * 0.03
            return CURSOR_MAX_SCL * (1 + wobble)
        else:
            t = (prog - 0.45) / 0.55
            e = ease_in_out_quad(t)
            return CURSOR_MAX_SCL - e * (CURSOR_MAX_SCL - 1.0)

    def _draw(self, handles: list, cx: int, cy: int, scale: float, prog: float) -> None:
        # ── 커서 본체 ────────────────────────────────────
        sz = CURSOR_BASE_PX * scale

        # 실제 커서(cx, cy)가 실제 Windows 커서 위에 그대로 보이도록
        # 확대 애니메이션 tip을 실제 커서보다 약간 아래-오른쪽으로 오프셋
        grow = (scale - 1.0) / (CURSOR_MAX_SCL - 1.0)   # 0~1
        tip_shift = sz * 0.18 * grow                     # 커질수록 더 멀어짐
        tx = cx - sz * 0.12 + tip_shift
        ty = cy - sz * 0.12 + tip_shift

        pts  = [(tx + nx * sz, ty + ny * sz) for nx, ny in CURSOR_NORM]
        flat = [c for p in pts for c in p]

        # 그림자
        off   = max(3, int(scale * 2.8))
        spts  = [(tx + off + nx * sz, ty + off + ny * sz) for nx, ny in CURSOR_NORM]
        sflat = [c for p in spts for c in p]
        sv    = max(12, int(90 * (1 - prog * 0.5)))
        h = self.canvas.create_polygon(*sflat, fill=f'#{sv:02x}{sv:02x}{sv:02x}', outline='')
        handles.append(h)

        # 흰색 본체
        h = self.canvas.create_polygon(*flat, fill='#ffffff', outline='')
        handles.append(h)

        # 검정 외곽선 (scale에 비례한 굵기)
        lw = max(2, int(2.5 * scale / CURSOR_MAX_SCL + 1.5))
        h  = self.canvas.create_polygon(*flat, fill='', outline='#111111', width=lw)
        handles.append(h)


# ══════════════════════════════════════════════════════════
#  설정 창
# ══════════════════════════════════════════════════════════
class SettingsWindow:
    def __init__(self, root: tk.Tk, config: dict, on_save):
        self._root   = root
        self.config  = config
        self.on_save = on_save
        self._win    = None

    def show(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.lift()
            return

        win = tk.Toplevel(self._root)
        self._win = win
        win.title('Mouse Finder 설정')
        win.resizable(False, False)
        win.attributes('-topmost', True)
        win.configure(padx=24, pady=20)

        hz = get_refresh_rate()
        tk.Label(win, text=f'모니터 주사율: {hz} Hz', fg='gray',
                 font=('Segoe UI', 9)).grid(row=0, column=0, columnspan=4,
                                             sticky='e', pady=(0, 8))

        # 감도
        tk.Label(win, text='흔들기 감도', font=('Segoe UI', 10, 'bold')).grid(
            row=1, column=0, columnspan=4, sticky='w', pady=(0, 4))

        tk.Label(win, text='낮음', fg='gray').grid(row=2, column=0, sticky='e')
        sens_var = tk.IntVar(value=self.config.get('sensitivity', 50))
        ttk.Scale(win, from_=10, to=100, orient='horizontal',
                  variable=sens_var, length=230).grid(row=2, column=1, padx=8)
        tk.Label(win, text='높음', fg='gray').grid(row=2, column=2, sticky='w')
        sens_lbl = tk.Label(win, text=str(sens_var.get()), width=4, anchor='w')
        sens_lbl.grid(row=2, column=3)
        sens_var.trace_add('write', lambda *_: sens_lbl.config(text=str(sens_var.get())))

        # 자동실행
        auto_var = tk.BooleanVar(value=get_autostart())
        tk.Checkbutton(win, text='Windows 시작 시 자동 실행',
                       variable=auto_var, font=('Segoe UI', 10)).grid(
            row=3, column=0, columnspan=4, sticky='w', pady=(16, 4))

        # 버튼
        bf = tk.Frame(win)
        bf.grid(row=4, column=0, columnspan=4, pady=(16, 0))

        def apply():
            cfg = {'sensitivity': sens_var.get(), 'autostart': auto_var.get()}
            set_autostart(auto_var.get())
            self.on_save(cfg)
            win.destroy()

        ttk.Button(bf, text='저장',   command=apply).pack(side='left', padx=6)
        ttk.Button(bf, text='취소', command=win.destroy).pack(side='left', padx=6)

        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w,  h  = win.winfo_width(),       win.winfo_height()
        win.geometry(f'+{(sw-w)//2}+{(sh-h)//2}')


# ══════════════════════════════════════════════════════════
#  시스템 트레이
# ══════════════════════════════════════════════════════════
def start_tray(open_settings_fn, quit_fn) -> None:
    try:
        import pystray
        from PIL import Image, ImageDraw

        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        d.ellipse([4, 4, 60, 60], fill=(255, 200, 0, 240), outline=(220, 120, 0, 255), width=4)
        d.ellipse([22, 22, 42, 42], fill=(255, 255, 255, 230))

        menu = pystray.Menu(
            pystray.MenuItem(f'{APP_NAME} 실행 중', None, enabled=False),
            pystray.MenuItem('설정', lambda icon, item: open_settings_fn()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('종료', lambda icon, item: quit_fn()),
        )
        icon = pystray.Icon(APP_NAME, img, APP_NAME, menu=menu)
        threading.Thread(target=icon.run, daemon=True).start()
    except ImportError:
        print('[MouseFinder] pystray/Pillow 없음 — Ctrl+C 로 종료하세요.')


# ══════════════════════════════════════════════════════════
#  진입점
# ══════════════════════════════════════════════════════════
def main() -> None:
    config  = load_config()
    overlay = CursorOverlay()
    settings = SettingsWindow(
        root    = overlay.root,
        config  = config,
        on_save = lambda cfg: (config.update(cfg), save_config(config)),
    )
    detector = ShakeDetector(overlay.trigger, config)
    detector.start()

    def open_settings():
        overlay.root.after(0, settings.show)

    def quit_app():
        overlay.root.after(0, overlay.root.destroy)

    start_tray(open_settings, quit_app)

    hz = get_refresh_rate()
    print(f'[MouseFinder] 실행 중 | 주사율: {hz}Hz | 프레임: {round(1000/hz)}ms | 감도: {config["sensitivity"]}')
    overlay.run()


if __name__ == '__main__':
    main()
