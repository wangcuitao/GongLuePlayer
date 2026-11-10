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
