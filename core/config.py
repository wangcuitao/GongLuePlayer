# -*- coding: utf-8 -*-
"""配置模块：负责加载 / 保存播放器的可设置项（JSON 持久化）。

配置保存在项目根目录的 config.json。若文件不存在或读取失败，
回退到 DEFAULT_CONFIG 默认值，保证程序始终可用。
"""
import json
import os
import sys

# 项目根目录：开发态为 core 的上一级；打包成 exe 后为 exe 所在目录
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 默认配置。修改这里可改变"恢复默认"后的值。
DEFAULT_CONFIG = {
    "auto_play_next": False,   # 是否自动播放下一集（默认否，即播完暂停在片尾）
    "default_alpha": 0.8,      # 默认透明度 80%
    "seek_step": 5,            # 快进/快退步长（秒）
    "volume_step": 5,          # 音量步长
    "hint_confirmed": False,   # 是否已确认过首次启动的快捷键说明
    "bg_color": "#ffffff",     # 窗口背景色（初始白色）
    "slider_color": "#FFC0CB", # 进度条滑块颜色（默认粉色）
    "remember_position": True, # 是否记住上次关闭的窗口位置
    "last_x": None,            # 上次关闭时的窗口 x 坐标
    "last_y": None,            # 上次关闭时的窗口 y 坐标
    "hotkeys": {               # 全局快捷键（keyboard 库键名）
        "play_pause": "`",
        "seek_back": "left",
        "seek_forward": "right",
        "volume_up": "up",
        "volume_down": "down",
        "next": "]",
        "episode_menu": "\\",   # 选集（P 右边第三个键，即 \ / 、）
        "play_clipboard": "ctrl+v",
        "quit": "ctrl+alt+q",
    },
}

# 可配置的动作列表：(key, 中文说明)。顺序即设置界面中的显示顺序。
HOTKEY_ACTIONS = [
    ("play_pause", "暂停 / 播放"),
    ("seek_back", "快退"),
    ("seek_forward", "快进"),
    ("volume_up", "音量 +"),
    ("volume_down", "音量 -"),
    ("next", "下一集"),
    ("episode_menu", "选集"),
    ("play_clipboard", "粘贴播放"),
    ("quit", "退出"),
]


def config_path():
    """返回 config.json 的绝对路径。"""
    return os.path.join(APP_DIR, "config.json")


def load_config():
    """加载配置；缺失 / 损坏时回退默认值。返回完整配置字典。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝，避免污染默认值
    path = config_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _deep_update(cfg, data)
    except Exception as exc:
        print(f"[config] 读取配置失败，使用默认值：{exc}")
    return cfg


def save_config(cfg):
    """保存配置到 config.json。返回是否成功。"""
    path = config_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:
        print(f"[config] 保存配置失败：{exc}")
        return False


def _deep_update(base, update):
    """递归合并 update 到 base（保留 base 中未提及的键）。"""
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
