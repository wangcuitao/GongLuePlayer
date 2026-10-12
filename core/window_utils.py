# -*- coding: utf-8 -*-
"""窗口工具模块：封装 Windows 原生窗口操作。

包含：
    - 获取 Tkinter 窗口原生句柄
    - 鼠标穿透（WS_EX_TRANSPARENT）开关
    - 屏幕 / 工作区尺寸获取
    - 窗口定位
"""
import win32api
import win32con
import win32gui

# 窗口默认尺寸（16:9 比例，宽高比严格等于 800:450）
WINDOW_WIDTH = 800
WINDOW_HEIGHT = 450


def get_hwnd(root):
    """获取 Tkinter 顶层窗口的原生句柄。

    必须先调用 root.update() 强制 Tk 生成真实句柄，否则 winfo_id()
    可能返回 0 或无效值。若 winfo_id() 返回的是子窗口，则向上取其
    父窗口作为真正的顶层窗口。
    """
    root.update_idletasks()
    root.update()
    hwnd = root.winfo_id()
    parent = win32gui.GetParent(hwnd)
    if parent:
        hwnd = parent
    return hwnd


def get_work_area():
    """获取桌面可用工作区（排除任务栏），返回 (left, top, right, bottom)。"""
    try:
        return win32gui.SystemParametersInfo(win32con.SPI_GETWORKAREA)
    except Exception:
        width = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        height = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        return 0, 0, width, height


def get_screen_size():
    """获取屏幕分辨率 (width, height)。"""
    width = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
    height = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
    return width, height


def set_click_through(hwnd, enabled):
    """开启 / 关闭鼠标穿透。

    原理：修改窗口扩展样式 GWL_EXSTYLE，加入 WS_EX_TRANSPARENT 后，
    鼠标事件会直接穿透窗口落到下层（游戏 / 桌面），实现"看得见但点不到"。
    修改样式后调用 SetWindowPos + SWP_FRAMECHANGED 强制立即生效，
    避免样式变更延迟导致穿透切换不及时。
    """
    try:
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if enabled:
            style |= win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED
        else:
            style &= ~win32con.WS_EX_TRANSPARENT
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style)
        # 强制框架重绘，确保穿透状态立刻生效
        win32gui.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
            win32con.SWP_NOZORDER | win32con.SWP_FRAMECHANGED,
        )
    except Exception as exc:
        print(f"[window_utils] 设置鼠标穿透失败：{exc}")


def is_click_through(hwnd):
    """查询窗口当前是否处于鼠标穿透状态。"""
    try:
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        return bool(style & win32con.WS_EX_TRANSPARENT)
    except Exception:
        return False


def move_window(hwnd, x, y, width, height):
    """移动并设置窗口位置 / 尺寸。"""
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, x, y, width, height,
                          win32con.SWP_SHOWWINDOW)


def position_bottom_left(hwnd, width=WINDOW_WIDTH, height=WINDOW_HEIGHT, margin=10):
    """计算窗口在屏幕左下角的坐标，返回 (x, y)。

    左下角定位逻辑：x 贴左、y = 工作区底部 - 窗口高度 - 边距，
    保留原实现不变，仅默认宽高改为 16:9（800x450）。
    """
    left, top, right, bottom = get_work_area()
    x = left + margin
    y = bottom - height - margin
    return x, y
