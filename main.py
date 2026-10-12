# -*- coding: utf-8 -*-
"""程序入口：攻略跟跑视频播放器（悬浮窗 + 鼠标穿透 + B 站跟跑播放）。

功能：
    - 无边框、置顶、半透明悬浮窗口，默认鼠标穿透，纯净显示视频；
    - 按住【左 Alt】后：鼠标在视频上半区 → 顶部悬浮层（标题滚动 1/3 +
      暂停/下一集快捷键 + 设置/关闭）；鼠标在视频下半区 → 底部悬浮层
      （可点击进度条 + 选集按钮）；
    - 按「、」键（P 右边第三个键）打开选集窗口，打开期间无需 Alt 即可用鼠标；
    - 粘贴 B 站链接即可播放，自动识别合集并从指定分 P 开始；
    - 窗口比例随视频自动适配（100% 重合，无黑边）；
    - 播放时在视频左上角显示当前画质 3 秒。

架构说明：
    Alt 交互用「主线程 15ms 轮询 GetAsyncKeyState(VK_LMENU)」驱动，穿透切换
    与拖动均在主线程同步完成；拖动用全局鼠标左键 + 光标坐标实现，视频区
    （mpv 子窗口覆盖）也能拖动；滚轮用全局 WH_MOUSE_LL 钩子捕获。

重要：keyboard 库实现全局快捷键需要【管理员权限】运行。
"""
import ctypes
import math
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
import tkinter as tk
import tkinter.colorchooser as colorchooser
import tkinter.font as tkfont
import win32api
import win32gui
from ctypes import wintypes
from tkinter import messagebox

from core import hotkey as hotkey_mod
from core import player as player_mod
from core import window_utils
from core import config as config_mod

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
APP_NAME = "攻略跟跑播放器"

WINDOW_WIDTH = window_utils.WINDOW_WIDTH    # 800
WINDOW_HEIGHT = window_utils.WINDOW_HEIGHT  # 450

ALPHA_MIN = 0.1
ALPHA_MAX = 1.0
ALPHA_STEP = 0.05

TOP_OVERLAY_H = 32
BOTTOM_OVERLAY_H = 28
ROUND_RADIUS = 14

RE_B23_SHORT = re.compile(r"https?://b23\.tv/[A-Za-z0-9]+")
RE_BILI_FULL = re.compile(r"https?://[^\s\"'<>]+bilibili\.com/[^\s\"'<>]+")

_HOTKEY_DISPLAY = {
    "left": "←",
    "right": "→",
    "up": "↑",
    "down": "↓",
    "ctrl+v": "Ctrl+V",
    "ctrl+alt+q": "Ctrl+Alt+Q",
    "\\": "、",
    "`": "`",
    "]": "]",
}


def _display_key(combo):
    return _HOTKEY_DISPLAY.get(combo, combo)


def _fmt_time(seconds):
    try:
        s = int(seconds)
        return f"{s // 60:02d}:{s % 60:02d}"
    except Exception:
        return "00:00"


def _hex_to_rgb(hex_color):
    hex_color = (hex_color or "").lstrip("#")
    if len(hex_color) != 6:
        return (255, 255, 255)
    try:
        return (int(hex_color[0:2], 16), int(hex_color[2:4], 16),
                int(hex_color[4:6], 16))
    except Exception:
        return (255, 255, 255)


def _is_light(hex_color):
    r, g, b = _hex_to_rgb(hex_color)
    return (0.299 * r + 0.587 * g + 0.114 * b) > 150


def _contrast_fg(hex_color):
    return "#111111" if _is_light(hex_color) else "#ffffff"


def _shade(hex_color, factor):
    r, g, b = _hex_to_rgb(hex_color)
    r = max(0, min(255, int(r * factor)))
    g = max(0, min(255, int(g * factor)))
    b = max(0, min(255, int(b * factor)))
    return f"#{r:02X}{g:02X}{b:02X}"


class ProgressBar(tk.Canvas):
    """自定义可点击进度条：窄滑块 + 自定义颜色。"""

    def __init__(self, master, on_seek=None, color="#FFC0CB", trough_color="#dddddd",
                 bg="#ffffff", height=16, slider_width=6, **kw):
        super().__init__(master, height=height, bg=bg,
                         highlightthickness=0, bd=0, **kw)
        self._color = color
        self._trough_color = trough_color
        self._slider_width = slider_width
        self._fraction = 0.0
        self._on_seek = on_seek
        self._dragging = False
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    @property
    def dragging(self):
        return self._dragging

    def set_fraction(self, frac):
        self._fraction = max(0.0, min(1.0, frac))
        self._redraw()

    def set_theme(self, bg=None, color=None, trough_color=None):
        if bg is not None:
            self.configure(bg=bg)
        if color is not None:
            self._color = color
        if trough_color is not None:
            self._trough_color = trough_color
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 2 or h <= 2:
            return
        cy = h // 2
        self.create_line(1, cy, w - 1, cy, fill=self._trough_color, width=3)
        sw = self._slider_width
        cx = sw / 2 + (w - sw) * self._fraction
        self.create_rectangle(cx - sw / 2, 2, cx + sw / 2, h - 2,
                              fill=self._color, outline="")

    def _frac_from_x(self, x):
        w = self.winfo_width()
        sw = self._slider_width
        usable = w - sw
        if usable <= 0:
            return 0.0
        return max(0.0, min(1.0, (x - sw / 2) / usable))

    def _on_press(self, e):
        self._dragging = True
        self.set_fraction(self._frac_from_x(e.x))

    def _on_drag(self, e):
        if self._dragging:
            self.set_fraction(self._frac_from_x(e.x))

    def _on_release(self, e):
        self._dragging = False
        self.set_fraction(self._frac_from_x(e.x))
        if self._on_seek:
            self._on_seek(self._fraction)


# ---------------------------------------------------------------------------
# 全局鼠标滚轮钩子（WH_MOUSE_LL）
# ---------------------------------------------------------------------------
WH_MOUSE_LL = 14
WM_MOUSEWHEEL = 0x020A


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", _POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", _POINT),
    ]


_LowLevelMouseProc = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class GlobalWheelHook:
    """按需安装的全局滚轮钩子（按住左 Alt 时启用，松开即卸载）。

    消息循环用阻塞式 GetMessageW 立即处理每条消息，避免高轮询率鼠标
    （如游戏鼠标）在轮询循环下出现指针延迟 / 乱飘。
    """

    def __init__(self, on_wheel):
        self._on_wheel = on_wheel  # 回调(delta: +1/-1)
        self._hook = None
        self._proc = None
        self._thread = None
        self._running = False
        self._thread_id = None
        self._installed = None

    @property
    def active(self):
        return self._running

    def _callback(self, n_code, w_param, l_param):
        try:
            if n_code == 0 and w_param == WM_MOUSEWHEEL:
                info = ctypes.cast(l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                delta = ctypes.c_short(info.mouseData >> 16).value
                self._on_wheel(1 if delta > 0 else -1)
        except Exception:
            pass
        return ctypes.windll.user32.CallNextHookEx(
            self._hook, n_code, w_param, l_param)

    def start(self):
        """安装全局滚轮钩子。返回 True 表示安装成功。"""
        if self._running:
            return True
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            # 关键：设置 restype/argtypes，避免 64 位句柄被截断导致安装失败
            user32.SetWindowsHookExW.restype = ctypes.c_void_p
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int, _LowLevelMouseProc, ctypes.c_void_p, wintypes.DWORD]
            user32.CallNextHookEx.restype = ctypes.c_long
            user32.CallNextHookEx.argtypes = [
                ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            user32.GetMessageW.restype = ctypes.c_int
            user32.GetMessageW.argtypes = [
                ctypes.POINTER(_MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
            user32.PostThreadMessageW.restype = wintypes.BOOL
            user32.PostThreadMessageW.argtypes = [
                wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            kernel32.GetModuleHandleW.restype = ctypes.c_void_p
            kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        except Exception as exc:
            print(f"[wheel] 设置函数签名失败：{exc}")
            return False
        self._proc = _LowLevelMouseProc(self._callback)
        self._installed = threading.Event()
        # 安装钩子 + 消息循环必须在同一条线程里进行
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._installed.wait(timeout=2.0)
        return self._running

    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        mod = kernel32.GetModuleHandleW(None)
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, mod, 0)
        if not self._hook:
            print("[wheel] 全局滚轮钩子安装失败（SetWindowsHookExW 返回 0）")
            self._running = False
            self._installed.set()
            return
        self._running = True
        self._installed.set()
        msg = _MSG()
        # 阻塞式消息循环：立即处理每条鼠标消息，避免高频鼠标事件堆积
        while self._running:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self):
        self._running = False
        if self._hook:
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            self._hook = None
        # 唤醒阻塞中的 GetMessageW，让线程退出
        if self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def show_native_warning(title, text):
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x30)
    except Exception:
        pass


TRANSPARENT = "#FF00FF"  # transparentcolor 使用的透明哨兵色


def _round_rect_points(x, y, w, h, r, n=16):
    """生成圆角矩形轮廓采样点（canvas smooth 多边形，消除锯齿）。"""
    pts = []

    def arc(cx, cy, a0, a1):
        for i in range(n + 1):
            a = math.radians(a0 + (a1 - a0) * i / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))

    arc(x + r, y + r, 180, 270)
    arc(x + w - r, y + r, 270, 360)
    arc(x + w - r, y + h - r, 0, 90)
    arc(x + r, y + h - r, 90, 180)
    return pts


class RoundCard:
    """圆角提示卡片：transparentcolor + Canvas 自绘，消除黑边锯齿。"""

    def __init__(self, parent, radius=ROUND_RADIUS, bg="#ffffff", fg="#111111",
                 font_family="Microsoft YaHei", font_size=10):
        self.radius = radius
        self.bg = bg
        self.fg = fg
        self.font_family = font_family
        self.font_size = font_size
        self._w = 0
        self._h = 0
        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-transparentcolor", TRANSPARENT)
        except Exception:
            pass
        self.win.configure(bg=TRANSPARENT)
        self.canvas = tk.Canvas(self.win, bg=TRANSPARENT, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        # 先隐藏，避免创建时在默认位置(左上角)闪现；show() 里再定位并显示
        self.win.withdraw()

    def set_content(self, text, button=None):
        """设置多行文字，可选按钮 (文本, 回调)。"""
        c = self.canvas
        c.delete("all")
        font = tkfont.Font(family=self.font_family, size=self.font_size)
        lines = (text or "").split("\n")
        max_w = max((font.measure(ln) for ln in lines), default=0)
        line_h = font.metrics("linespace")
        text_h = line_h * max(1, len(lines))
        pad_x = 18
        pad_y = 12
        btn_w = 0
        btn_h = 0
        if button:
            btn_text, _cb = button
            btn_w = font.measure(btn_text) + 26
            btn_h = int(self.font_size * 2.0)
        self._w = max(max_w, btn_w) + pad_x * 2
        self._h = text_h + pad_y * 2 + (btn_h + 10 if button else 0)
        c.configure(width=self._w, height=self._h)
        c.create_polygon(_round_rect_points(0, 0, self._w, self._h, self.radius),
                         smooth=True, fill=self.bg, outline="")
        ty = pad_y
        for ln in lines:
            c.create_text(pad_x, ty, text=ln, font=(self.font_family, self.font_size),
                          anchor="nw", fill=self.fg)
            ty += line_h
        if button:
            btn_text, cb = button
            by = self._h - btn_h - 8
            bx = self._w - btn_w - pad_x
            b = c.create_polygon(
                _round_rect_points(bx, by, btn_w, btn_h, btn_h // 2),
                smooth=True, fill="#3a6ea5", outline="")
            c.create_text(bx + btn_w / 2, by + btn_h / 2, text=btn_text,
                          font=(self.font_family, self.font_size), fill="#ffffff")
            c.tag_bind(b, "<Button-1>", lambda e: cb())

    def show(self, x, y):
        self.win.update_idletasks()
        self.win.geometry(f"{int(self._w)}x{int(self._h)}+{int(x)}+{int(y)}")
        self.win.update_idletasks()
        self.win.deiconify()
        self.win.lift()

    def hide(self):
        try:
            self.win.withdraw()
        except Exception:
            pass

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


class RoundedWindow:
    """统一风格的圆角无边框白底窗口：标题栏 + 关闭按钮 + 可拖动内容区。"""

    def __init__(self, parent, title, width, height, radius=ROUND_RADIUS, on_close=None):
        self.radius = radius
        self.width = width
        self.height = height
        self._on_close_cb = on_close
        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-transparentcolor", TRANSPARENT)
        except Exception:
            pass
        self.win.configure(bg=TRANSPARENT)
        self.canvas = tk.Canvas(self.win, bg=TRANSPARENT, highlightthickness=0, bd=0,
                                width=width, height=height)
        self.canvas.pack()
        self._draw(title)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self._drag_off = None
        # 居中到屏幕
        try:
            sw, sh = window_utils.get_screen_size()
            self.win.geometry(
                f"{width}x{height}+{(sw - width) // 2}+{(sh - height) // 2}")
        except Exception:
            self.win.geometry(f"{width}x{height}")

    def _draw(self, title):
        c = self.canvas
        c.delete("all")
        c.create_polygon(_round_rect_points(0, 0, self.width, self.height, self.radius),
                         smooth=True, fill="#ffffff", outline="#e3e3e3", width=1)
        c.create_text(20, 24, text=title, anchor="w",
                      font=("Microsoft YaHei", 11, "bold"), fill="#111111")
        close = c.create_text(self.width - 22, 24, text="×", anchor="center",
                              font=("Microsoft YaHei", 15), fill="#111111")
        c.tag_bind(close, "<Button-1>", lambda e: self._close())
        # 内容区（内缩半径，避免方形 Frame 盖住圆角）
        self.body = tk.Frame(self.win, bg="#ffffff")
        self.body.place(x=self.radius, y=42,
                        width=self.width - 2 * self.radius,
                        height=self.height - 42 - self.radius)

    def _close(self):
        """关闭窗口（先触发 on_close 回调，再销毁）。"""
        try:
            if self._on_close_cb:
                self._on_close_cb()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass

    def _on_press(self, e):
        self._drag_off = (e.x_root - self.win.winfo_x(),
                          e.y_root - self.win.winfo_y())

    def _on_drag(self, e):
        if self._drag_off:
            self.win.geometry(
                f"+{e.x_root - self._drag_off[0]}+{e.y_root - self._drag_off[1]}")

    def _on_release(self, e):
        self._drag_off = None


class GongLuePlayerApp:
    def __init__(self):
        self.config = config_mod.load_config()
        self.root = tk.Tk()
        self.root.withdraw()

        self._cmd_queue = queue.Queue()

        self.width = WINDOW_WIDTH
        self.height = WINDOW_HEIGHT
        self.alpha = float(self.config.get("default_alpha", 0.8))
        self.seek_step = int(self.config.get("seek_step", 5))
        self.volume_step = int(self.config.get("volume_step", 5))
        self.auto_play_next = bool(self.config.get("auto_play_next", False))

        self.collection = []
        self.current_index = -1
        self._alt_active = False
        self._drag_offset = None
        self.player = None

        self._top_visible = False
        self._bottom_visible = False
        self._menu_open = False
        self._settings_win = None
        self._bili_win = None
        self._startup_win = None
        self._episode_menu = None
        self._quality_card = None
        self._quality_after_id = None
        self._toast_card = None
        self._toast_after_id = None
        self._idle_win = None
        self._idle_lbl = None
        self._playing = False
        self._volume_win = None

        self._title_full = ""
        self._title_offset = 0
        self._title_max_chars = 18

        # 初始主题（_build_ui 中会用到）
        self.bg = self._valid_hex(self.config.get("bg_color"), "#ffffff")
        self.fg = _contrast_fg(self.bg)
        self._hover = _shade(self.bg, 0.9) if _is_light(self.bg) else _shade(self.bg, 1.3)

        self._build_ui()
        self._apply_theme()
        self._recalc_title_chars()
        self._init_hwnd()
        self._init_player()
        self._init_hotkeys()
        self._init_wheel_hook()
        self._init_mouse_bindings()

        window_utils.set_click_through(self.hwnd, True)

        # 初始位置：记住位置则恢复，否则首次启动屏幕居中
        try:
            lx = self.config.get("last_x")
            ly = self.config.get("last_y")
            if self.config.get("remember_position", True) and \
                    isinstance(lx, int) and isinstance(ly, int):
                x, y = lx, ly
            else:
                raise ValueError
        except Exception:
            sw, sh = window_utils.get_screen_size()
            x, y = (sw - self.width) // 2, (sh - self.height) // 2
        self.root.geometry(f"{self.width}x{self.height}+{x}+{y}")
        self.root.deiconify()

        self._poll_queue()
        self.root.after(15, self._poll_alt)
        self.root.after(200, self._poll_progress)
        self.root.after(300, self._tick_title)
        self.root.after(80, self._show_idle_hint)
        self.root.after(150, self._maybe_show_startup_hint)

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.alpha)
        self.root.configure(bg=self.bg)

        # 视频渲染区（填满整个窗口）
        self.video_frame = tk.Frame(self.root, bg=self.bg)
        self.video_frame.pack(fill="both", expand=True)

        self._build_top_overlay()
        self._build_bottom_overlay()

    def _build_top_overlay(self):
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.attributes("-alpha", self.alpha)
        top.configure(bg=self.bg)
        top.withdraw()
        self._top_overlay = top

        self.title_label = tk.Label(top, text="", bg=self.bg, fg=self.fg,
                                    anchor="w", font=("Microsoft YaHei", 10),
                                    width=self._title_max_chars)
        self.title_label.pack(side="left", fill="y", padx=(8, 0))

        self.top_hint_label = tk.Label(top, text=self._build_top_hint(), bg=self.bg,
                                       fg=self.fg, anchor="w",
                                       font=("Microsoft YaHei", 9))
        self.top_hint_label.pack(side="left", fill="both", expand=True, padx=10)

        self.close_btn = tk.Button(top, text="×", command=self.quit, bg=self.bg,
                                   fg=self.fg, activebackground="#f8d7da",
                                   relief="flat", bd=0, font=("Microsoft YaHei", 13),
                                   cursor="hand2")
        self.close_btn.pack(side="right", padx=2)
        self.settings_btn = tk.Button(top, text="设置", command=self._open_settings,
                                      bg=self.bg, fg=self.fg,
                                      activebackground=self._hover, relief="flat", bd=0,
                                      font=("Microsoft YaHei", 9), cursor="hand2")
        self.settings_btn.pack(side="right", padx=2)
        self._bind_hover(self.close_btn)
        self._bind_hover(self.settings_btn)

    def _build_bottom_overlay(self):
        bot = tk.Toplevel(self.root)
        bot.overrideredirect(True)
        bot.attributes("-topmost", True)
        bot.attributes("-alpha", self.alpha)
        bot.configure(bg=self.bg)
        bot.withdraw()
        self._bottom_overlay = bot

        self.progress = ProgressBar(bot, on_seek=self._seek_to_fraction,
                                    color="#FFC0CB", trough_color="#dddddd",
                                    bg=self.bg, height=16, slider_width=6)
        self.time_label = tk.Label(bot, text="00:00 / 00:00", bg=self.bg,
                                   fg=self.fg, font=("Microsoft YaHei", 8))
        self.episode_btn = tk.Button(bot, text="选集(、)", command=self._open_episode_menu,
                                     bg=self.bg, fg=self.fg, activebackground=self._hover,
                                     relief="flat", bd=0, font=("Microsoft YaHei", 9),
                                     cursor="hand2")
        self.pause_btn = tk.Button(bot, text="暂停", command=self._do_toggle_play_pause,
                                   bg=self.bg, fg=self.fg, activebackground=self._hover,
                                   relief="flat", bd=0, font=("Microsoft YaHei", 9),
                                   cursor="hand2")
        self.volume_btn = tk.Button(bot, text="音量", command=self._toggle_volume_panel,
                                    bg=self.bg, fg=self.fg, activebackground=self._hover,
                                    relief="flat", bd=0, font=("Microsoft YaHei", 9),
                                    cursor="hand2")

        self.pause_btn.pack(side="left", padx=4)
        self.progress.pack(side="left", fill="x", expand=True, padx=(2, 4))
        self.time_label.pack(side="left", padx=6)
        self.episode_btn.pack(side="right", padx=4)
        self.volume_btn.pack(side="right", padx=2)
        self._bind_hover(self.episode_btn)
        self._bind_hover(self.pause_btn)
        self._bind_hover(self.volume_btn)

    def _apply_theme(self):
        bg = self._valid_hex(self.config.get("bg_color"), "#ffffff")
        slider = self._valid_hex(self.config.get("slider_color"), "#FFC0CB")
        fg = _contrast_fg(bg)
        trough = _shade(bg, 0.82) if _is_light(bg) else _shade(bg, 1.6)
        hover = _shade(bg, 0.9) if _is_light(bg) else _shade(bg, 1.3)
        self.bg = bg
        self.fg = fg
        self._hover = hover
        for w in (self.root, self.video_frame, self._top_overlay, self._bottom_overlay,
                  self.title_label, self.top_hint_label, self.time_label):
            try:
                w.configure(bg=bg)
            except Exception:
                pass
        for w in (self.title_label, self.top_hint_label, self.time_label):
            try:
                w.configure(fg=fg)
            except Exception:
                pass
        for b in (self.settings_btn, self.close_btn, self.episode_btn,
                  self.pause_btn, self.volume_btn):
            try:
                b.configure(bg=bg, fg=fg, activebackground=hover)
            except Exception:
                pass
        try:
            self.progress.set_theme(bg=bg, color=slider, trough_color=trough)
        except Exception:
            pass

    def _valid_hex(self, s, fallback="#ffffff"):
        s = (s or "").strip().lstrip("#")
        if len(s) == 6:
            try:
                int(s, 16)
                return "#" + s.upper()
            except Exception:
                pass
        return fallback

    def _bind_hover(self, w):
        """给按钮加悬停变色反馈（仅改背景色，不影响文字与其他元素）。"""
        try:
            w.bind("<Enter>", lambda e: w.configure(bg=self._hover))
            w.bind("<Leave>", lambda e: w.configure(bg=self.bg))
        except Exception:
            pass

    def _update_pause_btn(self):
        try:
            if self.player is not None and self.player.is_paused():
                self.pause_btn.config(text="播放")
            else:
                self.pause_btn.config(text="暂停")
        except Exception:
            pass

    def _show_idle_hint(self):
        """无视频时，在播放器区域以文字形式长期显示提示（与主题一致的底色）。"""
        try:
            if self._idle_win is None:
                win = tk.Toplevel(self.root)
                win.overrideredirect(True)
                win.attributes("-topmost", True)
                win.configure(bg=self.bg)
                self._idle_lbl = tk.Label(
                    win, text="粘贴 B 站链接后按 Ctrl+V 播放视频",
                    bg=self.bg, fg=self.fg,
                    font=("Microsoft YaHei", 13))
                self._idle_lbl.pack(expand=True)
                self._idle_win = win
                win.withdraw()  # 先隐藏，避免左上角闪现
                win.update_idletasks()
                window_utils.set_click_through(win.winfo_id(), True)
            self._idle_lbl.configure(bg=self.bg, fg=self.fg)
            self._reposition_idle()
            self._idle_win.deiconify()
            self._idle_win.lift()
        except Exception as exc:
            print(f"[main] 显示空闲提示失败：{exc}")

    def _reposition_idle(self):
        """让空闲提示文字跟随主窗口，避免拖动时残影。"""
        try:
            if self._idle_win is None:
                return
            vx = self.video_frame.winfo_rootx()
            vy = self.video_frame.winfo_rooty()
            vw = self.video_frame.winfo_width()
            vh = self.video_frame.winfo_height()
            self._idle_win.geometry(f"{vw}x{vh}+{vx}+{vy}")
        except Exception:
            pass

    def _hide_idle_hint(self):
        try:
            if self._idle_win is not None:
                self._idle_win.withdraw()
        except Exception:
            pass

    def _recalc_title_chars(self):
        try:
            f = tkfont.Font(family="Microsoft YaHei", size=10)
            char_w = f.measure("字") or 12
            self._title_max_chars = max(6, (self.width // 3 - 16) // char_w)
            self.title_label.configure(width=self._title_max_chars)
        except Exception:
            self._title_max_chars = 18

    # ------------------------------------------------------------------
    # 句柄 / 播放器 / 快捷键 / 滚轮
    # ------------------------------------------------------------------
    def _init_hwnd(self):
        self.hwnd = window_utils.get_hwnd(self.root)
        self.video_hwnd = self.video_frame.winfo_id()

    def _init_player(self):
        try:
            self.player = player_mod.Player(
                self.video_hwnd,
                on_event=lambda *a: self._dispatch(self._on_player_event, *a),
            )
            # 视频区上的滚轮由 mpv 捕获，绑定到 mpv 按键（不挂全局钩子）
            self.player.bind_wheel(
                lambda: self._dispatch(self._do_wheel, 1),
                lambda: self._dispatch(self._do_wheel, -1),
            )
        except Exception as exc:
            self.player = None
            messagebox.showwarning(APP_NAME, f"播放内核初始化失败：\n{exc}")

    def _init_hotkeys(self):
        self.hotkeys = hotkey_mod.HotkeyManager(on_conflict=self._on_hotkey_conflict)
        self._hotkey_callbacks = {
            "play_pause": self._do_toggle_play_pause,
            "seek_back": self._do_seek_back,
            "seek_forward": self._do_seek_forward,
            "volume_up": self._do_volume_up,
            "volume_down": self._do_volume_down,
            "next": self._do_play_next,
            "episode_menu": self._open_episode_menu,
            "play_clipboard": self._do_play_from_clipboard,
            "quit": self._do_quit,
        }
        self._register_hotkeys_from_config()
        if not self.hotkeys.available:
            self.set_status("警告：keyboard 库不可用，全局快捷键失效", 5000)

    def _register_hotkeys_from_config(self):
        self.hotkeys.unregister_all()
        hk = self.config.get("hotkeys") or {}
        for action, desc in config_mod.HOTKEY_ACTIONS:
            combo = hk.get(action, "")
            cb = self._hotkey_callbacks.get(action)
            if combo and cb:
                self.hotkeys.register(combo, lambda cb=cb: self._dispatch(cb), desc)

    def _init_wheel_hook(self):
        """已弃用全局滚轮钩子（会拖慢高轮询率游戏鼠标）。
        滚轮调节透明度改用 Tk <MouseWheel> 绑定 + mpv 滚轮按键绑定实现。"""
        self._wheel_hook = None

    # ------------------------------------------------------------------
    # 跨线程调度
    # ------------------------------------------------------------------
    def _dispatch(self, fn, *args):
        self._cmd_queue.put(lambda: fn(*args))

    def _poll_queue(self):
        try:
            while True:
                fn = self._cmd_queue.get_nowait()
                try:
                    fn()
                except Exception as exc:
                    print(f"[main] 队列任务异常：{exc}")
        except queue.Empty:
            pass
        self.root.after(30, self._poll_queue)

    # ------------------------------------------------------------------
    # Alt 交互（仅左 Alt）
    # ------------------------------------------------------------------
    def _is_alt_down(self):
        try:
            return bool(win32api.GetAsyncKeyState(0xA4) & 0x8000)  # VK_LMENU 仅左Alt
        except Exception:
            return False

    def _poll_alt(self):
        try:
            alt_down = self._is_alt_down()
            if alt_down != self._alt_active:
                self._alt_active = alt_down
                if alt_down:
                    self._apply_click_through(False)
                else:
                    self._apply_click_through(True)
                    self._drag_offset = None
                    self._set_overlays_visible(False, False)
            if self._alt_active and not self._menu_open:
                self._update_overlays()
                self._handle_alt_drag()
        except Exception as exc:
            print(f"[main] Alt 轮询异常：{exc}")
        self.root.after(15, self._poll_alt)

    def _update_overlays(self):
        show_top = False
        show_bottom = False
        try:
            px, py = win32api.GetCursorPos()
            wx, wy = self.root.winfo_x(), self.root.winfo_y()
            ww, wh = self.root.winfo_width(), self.root.winfo_height()
            inside = wx <= px < wx + ww and wy <= py < wy + wh
            if inside:
                if (py - wy) < wh * 0.5:
                    show_top = True
                else:
                    show_bottom = True
        except Exception:
            pass
        self._set_overlays_visible(show_top, show_bottom)

    def _set_overlays_visible(self, show_top, show_bottom):
        try:
            changed = False
            if show_top != self._top_visible:
                self._top_visible = show_top
                changed = True
                if show_top:
                    self._top_overlay.deiconify()
                    self._top_overlay.lift()
                else:
                    self._top_overlay.withdraw()
            if show_bottom != self._bottom_visible:
                self._bottom_visible = show_bottom
                changed = True
                if show_bottom:
                    self._bottom_overlay.deiconify()
                    self._bottom_overlay.lift()
                else:
                    self._bottom_overlay.withdraw()
            if changed and (show_top or show_bottom):
                self._position_overlays()
        except Exception as exc:
            print(f"[main] 切换悬浮层失败：{exc}")

    def _position_overlays(self):
        try:
            wx, wy = self.root.winfo_x(), self.root.winfo_y()
            ww, wh = self.root.winfo_width(), self.root.winfo_height()
            self._top_overlay.geometry(f"{ww}x{TOP_OVERLAY_H}+{wx}+{wy}")
            self._bottom_overlay.geometry(
                f"{ww}x{BOTTOM_OVERLAY_H}+{wx}+{wy + wh - BOTTOM_OVERLAY_H}")
        except Exception:
            pass

    def _handle_alt_drag(self):
        lmb = win32api.GetAsyncKeyState(0x01) & 0x8000  # VK_LBUTTON
        cx, cy = win32api.GetCursorPos()
        wx, wy = self.root.winfo_x(), self.root.winfo_y()
        ww, wh = self.root.winfo_width(), self.root.winfo_height()
        if lmb and self._drag_offset is None:
            if not (wx <= cx < wx + ww and wy <= cy < wy + wh):
                return
            if self._cursor_on_overlay(cx, cy):
                return
            self._drag_offset = (cx - wx, cy - wy)
        elif lmb and self._drag_offset is not None:
            self.root.geometry(f"+{cx - self._drag_offset[0]}+{cy - self._drag_offset[1]}")
            self._position_overlays()
            self._reposition_idle()
        elif not lmb:
            self._drag_offset = None

    def _cursor_on_overlay(self, cx, cy):
        for ov in (self._top_overlay, self._bottom_overlay):
            try:
                if ov.winfo_ismapped():
                    ox, oy = ov.winfo_rootx(), ov.winfo_rooty()
                    ow, oh = ov.winfo_width(), ov.winfo_height()
                    if ox <= cx < ox + ow and oy <= cy < oy + oh:
                        return True
            except Exception:
                pass
        return False

    def _apply_click_through(self, enabled):
        try:
            window_utils.set_click_through(self.hwnd, enabled)
        except Exception as exc:
            print(f"[main] 切换鼠标穿透失败：{exc}")

    def _do_wheel(self, sign):
        """滚轮调节透明度（供 Tk 绑定与 mpv 滚轮按键共同调用）。"""
        if not self._alt_active:
            return
        self.set_alpha(self.alpha + sign * ALPHA_STEP)

    def _init_mouse_bindings(self):
        """滚轮调透明度：绑定到主窗口与各部件（按住左 Alt 时穿透已关，可收到滚轮）。"""
        for w in (self.root, self.video_frame, self._top_overlay, self._bottom_overlay):
            try:
                w.bind("<MouseWheel>", self._on_mouse_wheel)
            except Exception:
                pass

    def _on_mouse_wheel(self, event):
        if not self._alt_active:
            return
        sign = 1 if event.delta > 0 else -1
        self.set_alpha(self.alpha + sign * ALPHA_STEP)

    # ------------------------------------------------------------------
    # 进度条 / 选集
    # ------------------------------------------------------------------
    def _poll_progress(self):
        if self._bottom_visible and self.player and not self.progress.dragging:
            try:
                pos = self.player.get_position()
                dur = self.player.get_duration()
                if dur and dur > 0:
                    self.progress.set_fraction(pos / dur)
                    self.time_label.config(text=f"{_fmt_time(pos)} / {_fmt_time(dur)}")
            except Exception:
                pass
        self.root.after(200, self._poll_progress)

    def _seek_to_fraction(self, frac):
        if not self.player:
            return
        try:
            # 限制到 0.999，避免点到最右端触发 end_file 导致误跳集/重定位
            frac = max(0.0, min(0.999, frac))
            dur = self.player.get_duration()
            if dur and dur > 0:
                self.player.seek_absolute(frac * dur)
        except Exception as exc:
            print(f"[main] 进度条跳转失败：{exc}")

    def _open_episode_menu(self):
        if self._menu_open:
            self._close_episode_menu()  # 再次按下：关闭
            return
        if not self.collection:
            self.set_status("当前没有可选集")
            return
        self._menu_open = True
        self._apply_click_through(False)  # 打开菜单期间允许鼠标操作，无需 Alt
        menu = tk.Menu(self.root, tearoff=0, bg="#ffffff", fg="#111111",
                       activebackground="#e8e8e8", activeforeground="#111111")
        for i, entry in enumerate(self.collection):
            title = entry.get("title") or f"第 {i + 1} 集"
            if i == self.current_index:
                title = f"▶ {title}"
            menu.add_command(
                label=f"{i + 1}. {title}",
                command=lambda i=i: self._select_episode(i),
            )
        self._episode_menu = menu
        try:
            menu.update_idletasks()
            x = self.root.winfo_x() + self.root.winfo_width() - 20
            y = self.root.winfo_y() + self.root.winfo_height() - menu.winfo_reqheight() - 40
            menu.tk_popup(x, y)
        except Exception as exc:
            print(f"[main] 选集菜单弹出失败：{exc}")
            self._close_episode_menu()

    def _select_episode(self, index):
        self._close_episode_menu()
        if 0 <= index < len(self.collection):
            self._start_playback(self.collection[index], index, len(self.collection))

    def _close_episode_menu(self):
        try:
            if self._episode_menu is not None:
                self._episode_menu.unpost()
        except Exception:
            pass
        self._episode_menu = None
        self._menu_open = False
        self._apply_click_through(not self._alt_active)

    # ------------------------------------------------------------------
    # 音量面板
    # ------------------------------------------------------------------
    def _toggle_volume_panel(self):
        if self._volume_win is not None:
            try:
                if self._volume_win.winfo_exists():
                    self._close_volume_panel()
                    return
            except Exception:
                pass
        if not self.player:
            self.set_status("尚未播放视频")
            return
        # 打开音量面板：临时关闭穿透，无需按住 Alt 即可操作
        self._apply_click_through(False)
        slider_color = self._valid_hex(self.config.get("slider_color"), "#FFC0CB")
        rw = RoundedWindow(self.root, "音量", 260, 96, on_close=self._close_volume_panel)
        self._volume_win = rw.win
        vol = self.player.get_volume()
        self._volume_bar = ProgressBar(rw.body, on_seek=self._on_volume_seek,
                                       color=slider_color, trough_color="#dddddd",
                                       bg="#ffffff", height=20, slider_width=8)
        self._volume_bar.pack(fill="x", expand=True, pady=2)
        self._volume_bar.set_fraction(vol / 150.0)
        self._volume_pct = tk.Label(rw.body, text=f"{int(vol * 100 / 150)}%",
                                    bg="#ffffff", fg="#111111",
                                    font=("Microsoft YaHei", 9))
        self._volume_pct.pack(pady=(2, 0))
        try:
            wx = self.root.winfo_x() + self.root.winfo_width() + 8
            wy = self.root.winfo_y() + self.root.winfo_height() - 140
            self._volume_win.geometry(f"260x96+{wx}+{wy}")
        except Exception:
            pass

    def _on_volume_seek(self, frac):
        if not self.player:
            return
        vol = frac * 150.0
        self.player.set_volume(vol)
        try:
            self._volume_pct.config(text=f"{int(vol * 100 / 150)}%")
        except Exception:
            pass

    def _close_volume_panel(self):
        try:
            if self._volume_win is not None:
                self._volume_win.destroy()
        except Exception:
            pass
        self._volume_win = None
        self._apply_click_through(not self._alt_active)

    # ------------------------------------------------------------------
    # 提示文本
    # ------------------------------------------------------------------
    def _build_top_hint(self):
        hk = self.config.get("hotkeys") or {}
        pause = _display_key(hk.get("play_pause", ""))
        nxt = _display_key(hk.get("next", ""))
        return f"{pause} 暂停/播放    {nxt} 下一集"

    def _build_full_hint_text(self):
        hk = self.config.get("hotkeys") or {}
        rows = [
            (_display_key(hk.get("play_pause", "")), "暂停 / 播放"),
            (_display_key(hk.get("seek_back", "")), "快退"),
            (_display_key(hk.get("seek_forward", "")), "快进"),
            (_display_key(hk.get("volume_up", "")), "音量 +"),
            (_display_key(hk.get("volume_down", "")), "音量 -"),
            (_display_key(hk.get("next", "")), "下一集"),
            (_display_key(hk.get("episode_menu", "")), "选集（再次按下可关闭）"),
            (_display_key(hk.get("play_clipboard", "ctrl+v")), "粘贴视频链接即可播放"),
            (_display_key(hk.get("quit", "")), "退出"),
            ("左Alt+滚轮", "调节透明度"),
            ("左Alt+左键", "拖动窗口"),
        ]
        lines = ["【快捷键说明】"]
        for key, desc in rows:
            lines.append(f"  {key:<12} {desc}")
        return "\n".join(lines)

    def _obvious_shortcuts_text(self):
        hk = self.config.get("hotkeys") or {}
        return "\n".join([
            f"  {_display_key(hk.get('seek_back', ''))} / {_display_key(hk.get('seek_forward', ''))}    快退 / 快进",
            f"  {_display_key(hk.get('volume_up', ''))} / {_display_key(hk.get('volume_down', ''))}    音量 + / -",
            f"  {_display_key(hk.get('play_clipboard', 'ctrl+v'))}    粘贴视频链接即可播放",
            f"  {_display_key(hk.get('quit', ''))}    退出",
            f"  左Alt + 滚轮    调节透明度",
            f"  左Alt + 左键拖动    拖动窗口",
        ])

    def _key_label(self, canvas, text, x, y):
        """绘制带圆角的透明标签框，返回框底部中心坐标（用于画箭头）。"""
        f = tkfont.Font(family="Microsoft YaHei", size=10)
        tw = f.measure(text)
        th = f.metrics("linespace")
        padx = 12
        pady = 7
        w = tw + padx * 2
        h = th + pady * 2
        pts = _round_rect_points(x, y, w, h, 8)
        canvas.create_polygon(pts, smooth=True, fill="#ffffff", outline="#bbbbbb", width=1)
        canvas.create_text(x + w / 2, y + h / 2, text=text,
                           font=("Microsoft YaHei", 10), fill="#111111")
        return (x + w / 2, y + h)

    def _draw_keyboard(self, canvas):
        """绘制标准键盘并高亮需指认的按键，返回按键中心坐标字典。"""
        unit = 42
        gap = 5
        key_h = 42
        x0 = 30
        y0 = 95
        rows = [
            [("`",1),("1",1),("2",1),("3",1),("4",1),("5",1),("6",1),("7",1),("8",1),("9",1),("0",1),("-",1),("=",1),("⌫",1.8)],
            [("Tab",1.5),("Q",1),("W",1),("E",1),("R",1),("T",1),("Y",1),("U",1),("I",1),("O",1),("P",1),("[",1),("]",1),("\\",1.5)],
            [("Caps",1.8),("A",1),("S",1),("D",1),("F",1),("G",1),("H",1),("J",1),("K",1),("L",1),(";",1),("'",1),("Enter",2.2)],
            [("Shift",2.3),("Z",1),("X",1),("C",1),("V",1),("B",1),("N",1),("M",1),(",",1),(".",1),("/",1),("Shift",2.7)],
            [("Ctrl",1.4),("Win",1.2),("Alt",1.4),("Space",6),("Alt",1.4),("Menu",1.2),("Ctrl",1.4)],
        ]
        highlight = {"`": "#ffe0eb", "]": "#ffe0eb", "\\": "#ffe0eb"}
        centers = {}
        max_w = 0
        cy = y0
        for row in rows:
            cx = x0
            for label, wu in row:
                w = unit * wu + gap * (wu - 1)
                fill = highlight.get(label, "#ececec")
                pts = _round_rect_points(cx, cy, w, key_h, 6)
                canvas.create_polygon(pts, smooth=True, fill=fill, outline="#cccccc", width=1)
                canvas.create_text(cx + w / 2, cy + key_h / 2, text=label,
                                   font=("Microsoft YaHei", 10), fill="#111111")
                centers[label] = (cx + w / 2, cy + key_h / 2)
                cx += w + gap
            max_w = max(max_w, cx - gap)
            cy += key_h + gap
        canvas.configure(width=max_w, height=cy - gap)
        return centers

    def _maybe_show_startup_hint(self):
        # 每次启动播放器都弹出快捷键说明窗口
        try:
            win = tk.Toplevel(self.root)
            win.title("快捷键说明")
            win.configure(bg="#ffffff")
            win.resizable(False, False)
            win.attributes("-topmost", True)

            tk.Label(win, text="快捷键说明", bg="#ffffff", fg="#111111",
                     font=("Microsoft YaHei", 13, "bold")).pack(pady=(12, 2))
            tk.Label(win, text="图上标注了需要指认的按键，其余常用快捷键见下方文字。",
                     bg="#ffffff", fg="#888888",
                     font=("Microsoft YaHei", 9)).pack(pady=(0, 4))

            canvas = tk.Canvas(win, bg="#ffffff", highlightthickness=0, bd=0)
            canvas.pack(padx=14)
            centers = self._draw_keyboard(canvas)

            items = [
                ("`", "暂停 / 播放", 30),
                ("]", "下一集", 230),
                ("\\", "选集（再按一次可关闭）", 430),
            ]
            for key, text, lx in items:
                if key in centers:
                    bx, by = self._key_label(canvas, text, lx, 10)
                    kx, ky = centers[key]
                    canvas.create_line(bx, by + 4, kx, ky - 18,
                                       fill="#999999", width=1.5, arrow=tk.LAST,
                                       arrowshape=(9, 11, 4))

            tk.Label(win, text=self._obvious_shortcuts_text(), bg="#ffffff",
                     fg="#111111", justify="left", anchor="w",
                     font=("Microsoft YaHei", 10)).pack(anchor="w", padx=24, pady=(10, 0))

            tk.Button(win, text="确认",
                      command=lambda: self._dismiss_startup_hint(win),
                      bg="#3a6ea5", fg="#ffffff", relief="flat", padx=30, pady=4,
                      font=("Microsoft YaHei", 10), cursor="hand2").pack(
                anchor="e", padx=20, pady=(6, 16))

            win.update_idletasks()
            w = win.winfo_reqwidth()
            h = win.winfo_reqheight()
            sw, sh = window_utils.get_screen_size()
            win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
            win.lift()
            win.focus_force()
            self._startup_win = win
        except Exception as exc:
            print(f"[main] 显示启动提示失败：{exc}")
            try:
                messagebox.showinfo("快捷键说明", self._build_full_hint_text())
                self.config["hint_confirmed"] = True
                config_mod.save_config(self.config)
            except Exception:
                pass

    def _dismiss_startup_hint(self, win=None):
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        elif self._startup_win is not None:
            try:
                self._startup_win.destroy()
            except Exception:
                pass
        self._startup_win = None
        try:
            self.config["hint_confirmed"] = True
            config_mod.save_config(self.config)
        except Exception as exc:
            print(f"[main] 保存首次提示标记失败：{exc}")

    # ------------------------------------------------------------------
    # 标题滚动
    # ------------------------------------------------------------------
    def _tick_title(self):
        full = self._title_full or ""
        n = self._title_max_chars
        if not full:
            self.title_label.config(text="")
        elif len(full) <= n:
            self.title_label.config(text=full)
        else:
            padded = full + "    "
            start = self._title_offset % len(padded)
            self.title_label.config(text=padded[start:start + n])
            self._title_offset += 1
        self.root.after(300, self._tick_title)

    # ------------------------------------------------------------------
    # 设置窗口
    # ------------------------------------------------------------------
    def _open_settings(self):
        if self._settings_win is not None:
            try:
                if self._settings_win.winfo_exists():
                    self._settings_win.lift()
                    self._settings_win.focus_force()
                    return
            except Exception:
                pass
        rw = RoundedWindow(self.root, "设置", 580, 780)
        self._settings_win = rw.win

        self._cfg_auto_play = tk.BooleanVar(value=self.auto_play_next)
        self._cfg_remember_pos = tk.BooleanVar(
            value=self.config.get("remember_position", True))
        self._cfg_alpha = tk.DoubleVar(value=self.alpha)
        self._cfg_seek = tk.IntVar(value=self.seek_step)
        self._cfg_volume = tk.IntVar(value=self.volume_step)
        self._cfg_bg = tk.StringVar(value=self.config.get("bg_color", "#ffffff"))
        self._cfg_slider = tk.StringVar(value=self.config.get("slider_color", "#FFC0CB"))
        self._hotkey_vars = {}
        for action, _desc in config_mod.HOTKEY_ACTIONS:
            self._hotkey_vars[action] = tk.StringVar(
                value=self.config.get("hotkeys", {}).get(action, ""))

        body = rw.body

        row = 0
        tk.Label(body, text="播放设置", bg="#ffffff", fg="#111111",
                 font=("Microsoft YaHei", 10, "bold")).grid(
            row=row, column=0, columnspan=6, sticky="w", pady=(0, 6))
        row += 1
        tk.Checkbutton(body, text="自动播放下一集", variable=self._cfg_auto_play,
                       bg="#ffffff", fg="#111111", selectcolor="#ffffff",
                       activebackground="#ffffff", activeforeground="#111111",
                       anchor="w").grid(row=row, column=0, columnspan=6, sticky="w")
        row += 1
        tk.Checkbutton(body, text="记住上次关闭的窗口位置", variable=self._cfg_remember_pos,
                       bg="#ffffff", fg="#111111", selectcolor="#ffffff",
                       activebackground="#ffffff", activeforeground="#111111",
                       anchor="w").grid(row=row, column=0, columnspan=6, sticky="w")
        row += 1
        tk.Button(body, text="B站会员登录（高清画质）…", command=self._open_bili_login,
                  bg="#3a6ea5", fg="#ffffff", relief="flat", padx=10).grid(
            row=row, column=0, columnspan=6, sticky="w", pady=2)
        row += 1
        tk.Label(body, text="默认透明度 (0.1~1.0)", bg="#ffffff", fg="#333333").grid(
            row=row, column=0, sticky="w", pady=2)
        tk.Spinbox(body, from_=0.1, to=1.0, increment=0.05, textvariable=self._cfg_alpha,
                   width=6, bg="#ffffff", fg="#111111", insertbackground="#111111").grid(
            row=row, column=1, sticky="w", pady=2)
        row += 1
        tk.Label(body, text="快进快退步长 (秒)", bg="#ffffff", fg="#333333").grid(
            row=row, column=0, sticky="w", pady=2)
        tk.Spinbox(body, from_=1, to=60, increment=1, textvariable=self._cfg_seek,
                   width=6, bg="#ffffff", fg="#111111", insertbackground="#111111").grid(
            row=row, column=1, sticky="w", pady=2)
        row += 1
        tk.Label(body, text="音量步长", bg="#ffffff", fg="#333333").grid(
            row=row, column=0, sticky="w", pady=2)
        tk.Spinbox(body, from_=1, to=20, increment=1, textvariable=self._cfg_volume,
                   width=6, bg="#ffffff", fg="#111111", insertbackground="#111111").grid(
            row=row, column=1, sticky="w", pady=2)

        row += 1
        tk.Label(body, text="颜色设置", bg="#ffffff", fg="#111111",
                 font=("Microsoft YaHei", 10, "bold")).grid(
            row=row, column=0, columnspan=6, sticky="w", pady=(10, 4))
        row += 1
        self._make_color_row(body, row, "窗口背景色", self._cfg_bg,
                             config_mod.DEFAULT_CONFIG["bg_color"])
        row += 1
        self._make_color_row(body, row, "进度条滑块色", self._cfg_slider,
                             config_mod.DEFAULT_CONFIG["slider_color"])

        row += 1
        tk.Label(body, text="全局快捷键（点击右侧输入框后按组合键设置）",
                 bg="#ffffff", fg="#111111", font=("Microsoft YaHei", 10, "bold")).grid(
            row=row, column=0, columnspan=6, sticky="w", pady=(10, 4))
        row += 1
        self._hotkey_entries = {}
        for action, desc in config_mod.HOTKEY_ACTIONS:
            tk.Label(body, text=desc, bg="#ffffff", fg="#333333", width=14,
                     anchor="w").grid(row=row, column=0, sticky="w", pady=2)
            entry = tk.Entry(body, textvariable=self._hotkey_vars[action], width=16,
                             bg="#ffffff", fg="#111111", readonlybackground="#ffffff",
                             insertbackground="#111111", justify="center")
            entry.grid(row=row, column=1, sticky="w", pady=2)
            entry.config(state="readonly")
            entry.bind("<Button-1>",
                       lambda e, a=action, w=entry: self._start_record_hotkey(a, w))
            self._hotkey_entries[action] = entry
            row += 1
        tk.Label(body, text="提示：点击右侧快捷键框后，直接按下想要设置的键（支持组合键），自动识别保存。",
                 bg="#ffffff", fg="#999999", font=("Microsoft YaHei", 8)).grid(
            row=row, column=0, columnspan=6, sticky="w", pady=(4, 0))
        row += 1

        btn_row = tk.Frame(body, bg="#ffffff")
        btn_row.grid(row=row, column=0, columnspan=6, sticky="ew", pady=(10, 0))
        tk.Button(btn_row, text="恢复默认", command=self._reset_settings_defaults,
                  bg="#eeeeee", fg="#111111", relief="flat", padx=10).pack(side="left")
        tk.Button(btn_row, text="保存并应用", command=self._apply_settings,
                  bg="#3a6ea5", fg="#ffffff", relief="flat", padx=10).pack(side="right")
        tk.Button(btn_row, text="取消", command=self._close_settings,
                  bg="#eeeeee", fg="#111111", relief="flat", padx=10).pack(
            side="right", padx=4)

    def _make_color_row(self, body, row, label_text, var, default_hex):
        tk.Label(body, text=label_text, bg="#ffffff", fg="#333333", width=14,
                 anchor="w").grid(row=row, column=0, sticky="w", pady=2)
        tk.Entry(body, textvariable=var, width=10, bg="#ffffff", fg="#111111",
                 insertbackground="#111111").grid(row=row, column=1, sticky="w",
                                                  padx=(0, 4), pady=2)
        swatch = tk.Label(body, text="  ", bg="#000000", relief="solid", bd=1, width=2)
        swatch.grid(row=row, column=2, sticky="w", padx=(0, 6))
        tk.Button(body, text="调色盘", command=lambda: self._pick_color(var),
                  bg="#eeeeee", fg="#111111", relief="flat", padx=6).grid(
            row=row, column=3, padx=2)
        tk.Button(body, text="吸色笔", command=lambda: self._start_eyedropper(var),
                  bg="#eeeeee", fg="#111111", relief="flat", padx=6).grid(
            row=row, column=4, padx=2)
        tk.Button(body, text="默认", command=lambda: var.set(default_hex),
                  bg="#eeeeee", fg="#111111", relief="flat", padx=6).grid(
            row=row, column=5, padx=2)
        var.trace_add("write", lambda *a: swatch.configure(
            bg=self._valid_hex(var.get(), "#000000")))
        swatch.configure(bg=self._valid_hex(var.get(), "#000000"))

    def _pick_color(self, var):
        try:
            result = colorchooser.askcolor(color=self._valid_hex(var.get()),
                                           parent=self._settings_win)
            if result and result[1]:
                var.set(result[1])
        except Exception as exc:
            print(f"[main] 调色盘失败：{exc}")

    def _start_eyedropper(self, target_var):
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.25)
        try:
            w = win32api.GetSystemMetrics(0)
            h = win32api.GetSystemMetrics(1)
            win.geometry(f"{w}x{h}+0+0")
        except Exception:
            win.attributes("-fullscreen", True)
        win.configure(bg="#000000", cursor="crosshair")
        tk.Label(win, text="点击屏幕任意位置取色（Esc 取消）", bg="#000000",
                 fg="#ffffff", font=("Microsoft YaHei", 14)).pack(pady=60)

        def _on_pick(e):
            try:
                target_var.set(self._sample_screen_pixel(e.x_root, e.y_root))
            except Exception as exc:
                print(f"[main] 取色失败：{exc}")
            finally:
                win.destroy()

        win.bind("<Button-1>", _on_pick)
        win.bind("<Escape>", lambda e: win.destroy())
        win.focus_force()

    @staticmethod
    def _sample_screen_pixel(x, y):
        dc = win32gui.CreateDC("DISPLAY", None, None, None)
        try:
            c = win32gui.GetPixel(dc, int(x), int(y))
        finally:
            win32gui.DeleteDC(dc)
        r = (c >> 16) & 0xFF
        g = (c >> 8) & 0xFF
        b = c & 0xFF
        return f"#{r:02X}{g:02X}{b:02X}"

    def _reset_settings_defaults(self):
        d = config_mod.DEFAULT_CONFIG
        self._cfg_auto_play.set(d["auto_play_next"])
        self._cfg_remember_pos.set(d["remember_position"])
        self._cfg_alpha.set(d["default_alpha"])
        self._cfg_seek.set(d["seek_step"])
        self._cfg_volume.set(d["volume_step"])
        self._cfg_bg.set(d["bg_color"])
        self._cfg_slider.set(d["slider_color"])
        for action, _desc in config_mod.HOTKEY_ACTIONS:
            self._hotkey_vars[action].set(d["hotkeys"].get(action, ""))

    def _start_record_hotkey(self, action, entry):
        """点击快捷键框后，开始录制一次按键组合。"""
        kb = hotkey_mod.keyboard
        if kb is None:
            messagebox.showwarning(APP_NAME, "keyboard 库不可用，无法录制快捷键。")
            return
        # 临时禁用全局快捷键，避免录制时误触发已有功能
        self.hotkeys.unregister_all()
        entry.config(state="normal")
        entry.delete(0, tk.END)
        entry.insert(0, "按下快捷键…")
        entry.config(state="readonly")

        def _run():
            combo = None
            try:
                combo = kb.read_hotkey(suppress=False)
            except Exception as exc:
                print(f"[main] 录制快捷键失败：{exc}")
            self._dispatch(self._finish_record_hotkey, action, combo)

        threading.Thread(target=_run, daemon=True).start()

    def _finish_record_hotkey(self, action, combo):
        entry = self._hotkey_entries.get(action)
        if combo:
            self._hotkey_vars[action].set(combo)
        text = combo or self._hotkey_vars[action].get() or ""
        if entry is not None:
            entry.config(state="normal")
            entry.delete(0, tk.END)
            entry.insert(0, text)
            entry.config(state="readonly")
        # 恢复全局快捷键
        self._register_hotkeys_from_config()

    def _apply_settings(self):
        try:
            self.config["auto_play_next"] = bool(self._cfg_auto_play.get())
            self.config["remember_position"] = bool(self._cfg_remember_pos.get())
            self.config["default_alpha"] = max(0.1, min(1.0, float(self._cfg_alpha.get())))
            self.config["seek_step"] = max(1, min(60, int(self._cfg_seek.get())))
            self.config["volume_step"] = max(1, min(20, int(self._cfg_volume.get())))
            self.config["bg_color"] = self._valid_hex(self._cfg_bg.get(), "#ffffff")
            self.config["slider_color"] = self._valid_hex(self._cfg_slider.get(), "#FFC0CB")
            self.config["hotkeys"] = {
                action: self._hotkey_vars[action].get().strip()
                for action, _desc in config_mod.HOTKEY_ACTIONS
            }
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"设置输入无效：{exc}")
            return
        if config_mod.save_config(self.config):
            self._reload_from_config()
            self._close_settings()
            self.set_status("设置已保存并应用")
        else:
            messagebox.showerror(APP_NAME, "保存配置失败，请检查目录写入权限")

    def _close_settings(self):
        try:
            if self._settings_win is not None:
                self._settings_win.destroy()
        except Exception:
            pass
        self._settings_win = None

    def _reload_from_config(self):
        self.auto_play_next = bool(self.config.get("auto_play_next", False))
        self.seek_step = int(self.config.get("seek_step", 5))
        self.volume_step = int(self.config.get("volume_step", 5))
        self.set_alpha(float(self.config.get("default_alpha", 0.8)))
        self._register_hotkeys_from_config()
        self._apply_theme()
        try:
            self.top_hint_label.config(text=self._build_top_hint())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # B站会员登录（Cookie 提取）
    # ------------------------------------------------------------------
    def _open_bili_login(self):
        """打开「B站会员登录」二级界面，指导用浏览器扩展导出 cookie。"""
        if self._bili_win is not None:
            try:
                if self._bili_win.winfo_exists():
                    self._bili_win.lift()
                    self._bili_win.focus_force()
                    return
            except Exception:
                pass
        rw = RoundedWindow(self.root, "B站会员登录（高清画质）", 500, 440)
        self._bili_win = rw.win

        body = rw.body
        tk.Label(body, text="播放器通过读取程序目录下的 cookies.txt，以会员身份请求 B 站 1080P/4K 高清画质。",
                 bg="#ffffff", fg="#333333", wraplength=440, justify="left",
                 font=("Microsoft YaHei", 9)).pack(anchor="w", pady=(8, 4))

        # 步骤说明（浏览器扩展导出）
        f2 = tk.LabelFrame(body, text="操作步骤", bg="#ffffff",
                           fg="#111111", font=("Microsoft YaHei", 10), padx=10, pady=8)
        f2.pack(fill="x", pady=(6, 4))
        tk.Label(f2, text=(
            "1. 用 Chrome 或 Edge 浏览器登录 bilibili.com 并保持登录状态。\n"
            "2. 打开浏览器扩展商店，搜索并安装「Get cookies.txt LOCALLY」。\n"
            "3. 访问任意 bilibili.com 页面（如首页）。\n"
            "4. 点击浏览器右上角的扩展图标，选择「Export」（导出）。\n"
            "5. 把导出的文件重命名为 cookies.txt。\n"
            "6. 将 cookies.txt 放到程序所在目录（可点下方按钮打开）。\n\n"
            "放置后，下次粘贴播放视频时即会自动以会员身份请求最高画质。"),
            bg="#ffffff", fg="#333333", justify="left", wraplength=420,
            font=("Microsoft YaHei", 9)).pack(anchor="w")

        self._bili_status = tk.StringVar(value="")
        tk.Label(body, textvariable=self._bili_status, bg="#ffffff", fg="#b8860b",
                 font=("Microsoft YaHei", 9)).pack(anchor="w", pady=(6, 2))

        btns = tk.Frame(body, bg="#ffffff")
        btns.pack(fill="x", pady=(4, 0))
        tk.Button(btns, text="打开程序目录", command=self._do_open_app_dir,
                  bg="#eeeeee", fg="#111111", relief="flat", padx=10).pack(side="left")
        tk.Button(btns, text="检测 cookies.txt 是否就绪", command=self._do_check_cookies,
                  bg="#eeeeee", fg="#111111", relief="flat", padx=10).pack(
            side="left", padx=6)
        tk.Button(btns, text="关闭", command=lambda: self._bili_win.destroy(),
                  bg="#eeeeee", fg="#111111", relief="flat", padx=10).pack(side="right")

    def _do_check_cookies(self):
        path = self._cookies_path()
        if os.path.exists(path) and os.path.getsize(path) > 0:
            self._bili_status.set("cookies.txt 已就绪，下次播放视频时即会请求高清画质。")
        else:
            self._bili_status.set("未检测到 cookies.txt，请按上方步骤导出。")

    def _do_open_app_dir(self):
        try:
            os.startfile(os.path.dirname(self._cookies_path()))
        except Exception as exc:
            print(f"[main] 打开目录失败：{exc}")

    # ------------------------------------------------------------------
    # 播放控制
    # ------------------------------------------------------------------
    def _do_toggle_play_pause(self):
        if self.player:
            self.player.toggle_pause()
            self._update_pause_btn()

    def _do_seek_forward(self):
        if self.player:
            self.player.seek(self.seek_step)

    def _do_seek_back(self):
        if self.player:
            self.player.seek(-self.seek_step)

    def _do_volume_up(self):
        if self.player:
            self.player.volume_delta(self.volume_step)

    def _do_volume_down(self):
        if self.player:
            self.player.volume_delta(-self.volume_step)

    def _do_play_next(self):
        if self.collection and self.current_index + 1 < len(self.collection):
            self._start_playback(self.collection[self.current_index + 1],
                                 self.current_index + 1, len(self.collection))
        else:
            self.set_status("没有下一集")

    def _do_play_from_clipboard(self):
        try:
            text = self.root.clipboard_get().strip()
        except Exception:
            self.set_status("剪贴板为空或不可读取")
            return
        url = self._extract_bili_url(text)
        if url:
            self.play_from_url(url)
        else:
            self.set_status("剪贴板中未发现 B 站链接")

    def _do_quit(self):
        self.quit()

    @staticmethod
    def _extract_bili_url(text):
        m = RE_B23_SHORT.search(text)
        if m:
            return m.group(0)
        m = RE_BILI_FULL.search(text)
        if m:
            return m.group(0)
        return None

    def play_from_url(self, url):
        self.set_status("正在解析视频…")
        start_p = self._parse_start_p(url)
        clean_url = self._strip_p_param(url)
        threading.Thread(target=self._resolve_and_play, args=(clean_url, start_p),
                         daemon=True).start()

    @staticmethod
    def _parse_start_p(url):
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            p = qs.get("p", [None])[0]
            if p is None:
                return None
            idx = int(p) - 1
            return idx if idx >= 0 else 0
        except Exception:
            return None

    @staticmethod
    def _strip_p_param(url):
        """移除 URL 中的 p 参数，以便 yt-dlp 返回完整合集（而非单集）。"""
        try:
            parts = urllib.parse.urlsplit(url)
            qs = urllib.parse.parse_qs(parts.query)
            qs.pop("p", None)
            new_query = urllib.parse.urlencode(qs, doseq=True)
            return urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
        except Exception:
            return url

    def _cookies_path(self):
        """返回 cookies.txt 路径（用于会员高清画质）。打包后写 exe 同级目录。"""
        if getattr(sys, "frozen", False):
            return os.path.join(os.path.dirname(sys.executable), "cookies.txt")
        return os.path.join(config_mod.APP_DIR, "cookies.txt")

    def _resolve_and_play(self, url, start_p=None):
        try:
            resolver = player_mod.BiliResolver(cookies_file=self._cookies_path())
            info = resolver.resolve(url)
            entries = player_mod.extract_playable_list(info)
        except Exception as exc:
            self._dispatch(self._on_error, f"解析失败：{exc}")
            return
        if not entries:
            self._dispatch(self._on_error, "未解析出可播放内容")
            return
        self.collection = entries
        start_idx = 0
        if start_p is not None and 0 <= start_p < len(entries):
            start_idx = start_p
        self._dispatch(self._start_playback, entries[start_idx], start_idx, len(entries))

    def _start_playback(self, entry, index, total):
        if not self.player:
            self._on_error("播放内核未初始化")
            return
        try:
            self._fit_window_to_video(entry)
            self.player.play_entry(entry)
            self.current_index = index
            self._playing = True
            self._hide_idle_hint()
            title = entry.get("title") or "未命名"
            self._title_full = f"[{index + 1}/{total}] {title}"
            self._title_offset = 0
            self._show_quality_label(entry)
            self._update_pause_btn()
        except Exception as exc:
            self._on_error(f"播放失败：{exc}")

    def _fit_window_to_video(self, entry):
        """按视频宽高比调整窗口，使其与画面 100% 重合（无黑边）。"""
        w = entry.get("width")
        h = entry.get("height")
        if not w or not h:
            return
        try:
            ratio = w / h
            work = window_utils.get_work_area()
            max_w = max(320, work[2] - work[0] - 40)
            max_h = max(200, work[3] - work[1] - 40)
            new_w = WINDOW_WIDTH
            new_h = round(new_w / ratio)
            if new_h > max_h:
                new_h = max_h
                new_w = round(new_h * ratio)
            if new_w > max_w:
                new_w = max_w
                new_h = round(new_w / ratio)
            new_w = max(200, new_w)
            new_h = max(120, new_h)
            self.width, self.height = new_w, new_h
            # 只改尺寸、保持当前位置，不再自动回左下角
            self.root.geometry(f"{new_w}x{new_h}")
            self._recalc_title_chars()
            self._position_overlays()
        except Exception as exc:
            print(f"[main] 调整窗口比例失败：{exc}")

    def _on_player_event(self, event_name, *args):
        if event_name == "end_file":
            if self.auto_play_next and self.current_index + 1 < len(self.collection):
                nxt = self.collection[self.current_index + 1]
                self._start_playback(nxt, self.current_index + 1, len(self.collection))
        elif event_name == "no_audio":
            detail = args[0] if args else ""
            self.set_status(f"未检测到音轨 {detail}")

    # ------------------------------------------------------------------
    # 画质标签 / 提示
    # ------------------------------------------------------------------
    @staticmethod
    def _quality_label(entry):
        note = entry.get("format_note") or ""
        if note:
            return note
        height = entry.get("height")
        if height:
            return f"{height}P"
        return "自动"

    def _show_quality_label(self, entry):
        label = self._quality_label(entry)
        try:
            if self._quality_card is None:
                self._quality_card = RoundCard(self.root, font_size=11)
            self._quality_card.set_content(f"画质：{label}")
            vx = self.video_frame.winfo_rootx()
            vy = self.video_frame.winfo_rooty()
            self._quality_card.show(vx + 10, vy + 10)
            if self._quality_after_id is not None:
                try:
                    self.root.after_cancel(self._quality_after_id)
                except Exception:
                    pass
            self._quality_after_id = self.root.after(3000, self._hide_quality_label)
        except Exception as exc:
            print(f"[main] 显示画质标签失败：{exc}")

    def _hide_quality_label(self):
        self._quality_after_id = None
        if self._quality_card is not None:
            self._quality_card.hide()

    def _show_toast(self, msg, duration_ms):
        try:
            if self._toast_card is None:
                self._toast_card = RoundCard(self.root, font_size=10)
            self._toast_card.set_content(msg)
            vx = self.video_frame.winfo_rootx()
            vy = self.video_frame.winfo_rooty()
            vw = self.video_frame.winfo_width()
            tw = self._toast_card._w
            self._toast_card.show(vx + (vw - tw) // 2, vy + 12)
            if self._toast_after_id is not None:
                try:
                    self.root.after_cancel(self._toast_after_id)
                except Exception:
                    pass
            self._toast_after_id = self.root.after(duration_ms, self._hide_toast)
        except Exception as exc:
            print(f"[main] 显示提示失败：{exc}")

    def _hide_toast(self):
        self._toast_after_id = None
        if self._toast_card is not None:
            self._toast_card.hide()

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def set_alpha(self, value):
        self.alpha = max(ALPHA_MIN, min(ALPHA_MAX, value))
        try:
            self.root.attributes("-alpha", self.alpha)
            self._top_overlay.attributes("-alpha", self.alpha)
            self._bottom_overlay.attributes("-alpha", self.alpha)
        except Exception as exc:
            print(f"[main] 设置透明度失败：{exc}")
        self.set_status(f"透明度 {int(self.alpha * 100)}%")

    def set_status(self, msg, duration_ms=2000):
        self._show_toast(msg, duration_ms)

    def _on_error(self, msg):
        print(f"[main] {msg}")
        self.set_status(msg, 6000)

    def _on_hotkey_conflict(self, combo, existing_desc, new_desc):
        messagebox.showwarning(
            APP_NAME,
            f"快捷键冲突：{combo} 已被“{existing_desc}”占用，无法再注册为“{new_desc}”。",
        )

    def quit(self):
        try:
            self.hotkeys.unregister_all()
        except Exception:
            pass
        try:
            if self.player:
                self.player.quit()
        except Exception:
            pass
        try:
            self.config["last_x"] = self.root.winfo_x()
            self.config["last_y"] = self.root.winfo_y()
            config_mod.save_config(self.config)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    if not is_admin():
        show_native_warning(
            APP_NAME,
            "当前未以管理员权限运行。\n\n"
            "全局快捷键（keyboard 库）需要管理员权限才能正常工作。\n"
            "请右键程序 → “以管理员身份运行”。",
        )
    app = GongLuePlayerApp()
    app.root.mainloop()


if __name__ == "__main__":
    main()
