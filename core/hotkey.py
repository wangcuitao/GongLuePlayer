# -*- coding: utf-8 -*-
"""全局快捷键模块：基于 keyboard 库实现窗口失焦时的全局按键监听。

注意：
    keyboard 库在 Windows 上需要管理员权限才能捕获全局按键。
    本模块做了 ImportError 与运行时异常的双重兜底，保证在无权限 /
    未安装时程序其余功能（穿透、拖动）仍可正常工作。
"""
import threading

try:
    import keyboard
except Exception:  # 可能因权限不足或未安装而失败
    keyboard = None


class HotkeyManager:
    """全局快捷键管理器，内置简单的冲突检测。"""

    def __init__(self, on_conflict=None):
        """on_conflict(combo, existing_desc, new_desc)：冲突时的回调。"""
        self._on_conflict = on_conflict
        self._registered = {}          # combo -> (callback, description)
        self._lock = threading.Lock()

    @property
    def available(self):
        """keyboard 库是否可用。"""
        return keyboard is not None

    def register(self, combo, callback, description=""):
        """注册全局快捷键。

        返回 True 表示注册成功；若与已注册的键冲突或注册失败返回 False。
        """
        if keyboard is None:
            return False
        with self._lock:
            if combo in self._registered:
                existing_desc = self._registered[combo][1]
                if self._on_conflict:
                    self._on_conflict(combo, existing_desc, description)
                return False
        try:
            keyboard.add_hotkey(combo, callback, suppress=False)
        except Exception as exc:
            print(f"[hotkey] 注册 {combo} 失败：{exc}")
            return False
        with self._lock:
            self._registered[combo] = (callback, description)
        return True

    def unregister_all(self):
        """移除全部已注册快捷键。"""
        with self._lock:
            combos = list(self._registered.keys())
            self._registered.clear()
        for combo in combos:
            try:
                keyboard.remove_hotkey(combo)
            except Exception:
                pass

    @staticmethod
    def is_pressed(key):
        """判断某键当前是否被按下。"""
        if keyboard is None:
            return False
        try:
            return keyboard.is_pressed(key)
        except Exception:
            return False

    def get_hotkey_map(self):
        """返回所有快捷键的中文说明字典（供提示浮层动态显示）。

        动态读取 self._registered 中已注册的快捷键，把键盘键名翻译为
        易读符号（箭头、组合键大写）；再补充 Alt+滚轮、Alt+左键 两个
        鼠标快捷操作（这两个不经过 keyboard 注册表，因此单独追加）。
        这样日后在 main.py 增删快捷键时，浮层会自动跟着更新。
        """
        # 键盘键名 -> 显示符号
        label_map = {
            "left": "←",
            "right": "→",
            "up": "↑",
            "down": "↓",
            "ctrl+v": "Ctrl+V",
            "ctrl+alt+q": "Ctrl+Alt+Q",
        }
        with self._lock:
            items = list(self._registered.items())
        result = {}
        for combo, (callback, desc) in items:
            label = label_map.get(combo, combo)
            result[label] = desc
        # 鼠标类快捷操作（不属于 keyboard 注册表，单独补充）
        result["Alt+滚轮"] = "调节透明度"
        result["Alt+左键"] = "拖动窗口"
        return result
