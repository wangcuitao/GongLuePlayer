# -*- coding: utf-8 -*-
"""播放器模块：封装 MPV 播放内核，以及基于 yt-dlp 的 B 站视频流解析。

本模块职责：
    1. 在 import mpv 之前，将 libmpv-2.dll 所在目录加入 DLL 搜索路径；
    2. 提供 Player 类，封装 MPV 的播放 / 暂停 / 快进 / 音量等操作；
    3. 提供 BiliResolver 类，使用 yt-dlp 在子线程中解析 B 站真实流地址，
       并自动识别合集（多 P / 播放列表）。
"""
import os
import sys
import threading
import time


# ---------------------------------------------------------------------------
# 第一步：确保 libmpv-2.dll 可被 ctypes 找到（必须在 import mpv 之前执行）
# ---------------------------------------------------------------------------
def _setup_libmpv_path():
    """将 libmpv-2.dll 所在目录加入 Windows 的 DLL 搜索路径。"""
    if sys.platform != "win32":
        return
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(sys._MEIPASS)
    here = os.path.dirname(os.path.abspath(__file__))          # .../core
    candidates.append(here)
    candidates.append(os.path.dirname(here))                   # 项目根目录
    for directory in candidates:
        dll = os.path.join(directory, "libmpv-2.dll")
        if os.path.exists(dll):
            try:
                os.add_dll_directory(directory)
            except Exception:
                pass
            os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
            break


_setup_libmpv_path()

try:
    import mpv
except Exception as exc:      # 可能是 libmpv-2.dll 未放置导致
    mpv = None
    MPV_IMPORT_ERROR = exc

import yt_dlp


class Player:
    """MPV 播放器封装。

    通过 python-mpv 调用 libmpv-2.dll，实现嵌入式视频播放。
    所有对 mpv 的调用都包裹在 try-except 中，避免底层异常导致程序崩溃。
    """

    def __init__(self, hwnd, on_event=None):
        """初始化播放器。

        参数:
            hwnd: Tkinter 视频区域 Frame 的原生句柄（winfo_id() 返回值）。
            on_event: 事件回调函数，接收字符串事件名（如 "end_file"）。
        """
        if mpv is None:
            raise RuntimeError(
                f"无法加载 libmpv-2.dll，请确认已放置到项目根目录。"
                f"原始错误：{MPV_IMPORT_ERROR}"
            )
        self.on_event = on_event
        self._hwnd = hwnd
        try:
            self._mpv = mpv.MPV(
                wid=str(hwnd),                 # 渲染到指定窗口句柄
                keep_open="yes",               # 播完停在片尾（暂停在最后一帧）
                ytdl=False,                    # 禁用 mpv 内置 ytdl，统一走 yt-dlp
                vo="gpu",                      # 视频输出后端
                hwdec="auto-safe",             # 硬解失败自动回退软解（修复偶发不播）
                cache="yes",                   # 开启网络缓存，减少卡顿
                demuxer_max_bytes="128MiB",    # 提高缓存，减少网络卡顿
                demuxer_readahead_secs="30",
                input_default_bindings=False,  # 关闭 mpv 默认按键绑定
                osc=False,                     # 关闭 mpv 自带 OSC 控制条
                input_cursor_passthrough=True, # 鼠标事件穿透到 Tk 窗口
                # ---- 高画质缩放 / 着色（达到最佳观感）----
                scale="ewa_lanczossharp",      # 高质量放大算法
                cscale="ewa_lanczossoft",      # 色度缩放
                dscale="mitchell",             # 缩小算法
                deband="yes",                  # 去色带
                deband_iterations=2,
                deband_threshold=35,
                deband_grain=4,
                dither_depth="auto",           # 抖动深度
                correct_downscaling="yes",
                linear_downscaling="yes",
                sigmoid_upscaling="yes",       # 提升低分辨率源的观感
            )
        except Exception as exc:
            raise RuntimeError(f"MPV 初始化失败：{exc}")

        self._recent_logs = []
        self._last_audio_url = None
        try:
            self._mpv.register_event_callback(self._on_mpv_event)
        except Exception as exc:
            print(f"[player] 注册事件回调失败：{exc}")
        try:
            self._mpv.log_handler = self._on_mpv_log
        except Exception as exc:
            print(f"[player] 设置日志回调失败：{exc}")

    # ------------------------------------------------------------------
    # 事件回调 / 日志
    # ------------------------------------------------------------------
    def _on_mpv_log(self, loglevel, namespace, message):
        """收集 mpv 的 warn/error 日志，用于诊断。"""
        try:
            if loglevel in ("fatal", "error", "warn"):
                self._recent_logs.append(f"[{loglevel}] {namespace}: {message}")
                if len(self._recent_logs) > 30:
                    self._recent_logs.pop(0)
        except Exception:
            pass

    def _on_mpv_event(self, event):
        """处理 mpv 底层事件，转发为简化的事件名。"""
        try:
            if event.event_id == mpv.MpvEventID.END_FILE:
                if self.on_event:
                    self.on_event("end_file")
        except Exception:
            pass

    def bind_wheel(self, on_wheel_up, on_wheel_down):
        """把鼠标滚轮绑定为 mpv 按键回调（用于视频区上的滚轮调节，无需全局钩子）。"""
        try:
            self._mpv.register_key_binding('WHEEL_UP', on_wheel_up, mode='force')
            self._mpv.register_key_binding('WHEEL_DOWN', on_wheel_down, mode='force')
        except Exception as exc:
            print(f"[player] 绑定滚轮失败：{exc}")

    # ------------------------------------------------------------------
    # 播放控制
    # ------------------------------------------------------------------
    def play_entry(self, entry):
        """播放一条由 yt-dlp 解析出的条目。

        参数:
            entry: yt-dlp info dict（单条视频），内含 formats/url 等字段。
        """
        video_url, audio_url, headers = self._extract_streams(entry)
        if not video_url:
            raise RuntimeError("未解析到可播放的视频流地址")

        # 防盗链：用 http-header-fields 确保 Referer / User-Agent 对视频与音频流都生效
        referer = headers.get("Referer") or "https://www.bilibili.com"
        user_agent = headers.get("User-Agent") or ""
        fields = [f"Referer: {referer}"]
        if user_agent:
            fields.append(f"User-Agent: {user_agent}")
        try:
            self._mpv["http-header-fields"] = ",".join(fields)
            self._mpv["referrer"] = referer
            if user_agent:
                self._mpv["user-agent"] = user_agent
        except Exception as exc:
            print(f"[player] 设置请求头失败：{exc}")

        self._last_audio_url = audio_url
        self._recent_logs = []

        # DASH 分离流：先播放视频，等装载完成后用 audio-add 添加外部音轨。
        # 切集时 play 是异步的，立即 audio-add 会因视频未就绪而报 -12。
        try:
            self._mpv.play(video_url)
        except Exception as exc:
            raise RuntimeError(f"播放失败：{exc}") from exc
        if audio_url:
            def _add_audio():
                # 等待视频装载完成（duration 有值），再添加音轨；失败重试
                for _ in range(30):
                    try:
                        if self._mpv.duration is not None and self._mpv.duration > 0:
                            break
                    except Exception:
                        pass
                    time.sleep(0.1)
                for attempt in range(3):
                    try:
                        self._mpv.command("audio-add", audio_url, "select")
                        return
                    except Exception as exc:
                        print(f"[player] audio-add 第{attempt + 1}次失败：{exc}")
                        time.sleep(0.3)
            threading.Thread(target=_add_audio, daemon=True).start()

        # 显式取消暂停，确保进入播放态（修复偶发的"解析后未播放"）
        try:
            self._mpv.pause = False
        except Exception:
            pass

        # 诊断：1 秒后检查是否加载到音轨
        try:
            threading.Timer(1.0, self._check_audio).start()
        except Exception:
            pass

    def _check_audio(self):
        """播放后检查是否加载到音轨，若无则通过 on_event 上报详情。"""
        try:
            tracks = self._mpv.track_list or []
            has_audio = any(t.get("type") == "audio" for t in tracks)
            if not has_audio and self.on_event:
                au = self._last_audio_url
                detail = f"audio_url={str(au)[:80]}" if au else "audio_url=无"
                if self._recent_logs:
                    detail += " | " + " ".join(self._recent_logs[-4:])
                self.on_event("no_audio", detail)
        except Exception as exc:
            print(f"[player] 检查音轨失败：{exc}")

    @staticmethod
    def _extract_streams(entry):
        """从单条 info 中提取视频流、音频流地址与请求头。"""
        headers = {}
        video_url = None
        audio_url = None

        # 顶层请求头优先（yt-dlp 会给出 Referer / User-Agent）
        headers.update(entry.get("http_headers") or {})

        # requested_formats 仅在 DASH 分离流（bestvideo+bestaudio）时存在
        requested = entry.get("requested_formats")
        if requested:
            for fmt in requested:
                if not isinstance(fmt, dict):
                    continue
                headers.update(fmt.get("http_headers") or {})
                vcodec = fmt.get("vcodec")
                if vcodec in (None, "", "none"):
                    # 无视频编码 → 纯音频流（B 站音频流的 acodec 可能为空，不能依赖它）
                    if not audio_url:
                        audio_url = fmt.get("url")
                else:
                    # 有视频编码 → 视频流
                    if not video_url:
                        video_url = fmt.get("url")

        # 若分离流提取不全，从完整 formats 列表回退查找
        if not video_url or not audio_url:
            for fmt in entry.get("formats") or []:
                if not isinstance(fmt, dict):
                    continue
                vcodec = fmt.get("vcodec")
                if vcodec in (None, "", "none"):
                    if not audio_url:
                        audio_url = fmt.get("url")
                else:
                    if not video_url:
                        video_url = fmt.get("url")
                if video_url and audio_url:
                    break

        # 单条合并流时直接取 url
        if not video_url:
            video_url = entry.get("url")

        return video_url, audio_url, headers

    def toggle_pause(self):
        """暂停 / 播放切换。"""
        try:
            self._mpv.pause = not self._mpv.pause
        except Exception as exc:
            print(f"[player] 切换暂停失败：{exc}")

    def is_paused(self):
        """查询当前是否处于暂停状态。"""
        try:
            return bool(self._mpv.pause)
        except Exception:
            return False

    def pause(self):
        try:
            self._mpv.pause = True
        except Exception as exc:
            print(f"[player] 暂停失败：{exc}")

    def resume(self):
        try:
            self._mpv.pause = False
        except Exception as exc:
            print(f"[player] 继续播放失败：{exc}")

    def seek(self, seconds):
        """相对快进 / 快退（秒），负数表示后退。"""
        try:
            self._mpv.seek(seconds, reference="relative")
        except Exception as exc:
            print(f"[player] 快进失败：{exc}")

    def seek_absolute(self, seconds):
        """跳转到绝对时间（秒），供进度条点击跳转使用。"""
        try:
            self._mpv.seek(seconds, reference="absolute")
        except Exception as exc:
            print(f"[player] 跳转进度失败：{exc}")

    def volume_delta(self, delta):
        """音量增减，范围钳制在 0~150。"""
        try:
            volume = self._mpv.volume
            self._mpv.volume = max(0, min(150, volume + delta))
        except Exception as exc:
            print(f"[player] 调节音量失败：{exc}")

    def get_volume(self):
        """获取当前音量（0~150），失败返回 100。"""
        try:
            v = self._mpv.volume
            return v if v is not None else 100.0
        except Exception:
            return 100.0

    def set_volume(self, value):
        """设置音量（0~150）。"""
        try:
            self._mpv.volume = max(0, min(150, value))
        except Exception as exc:
            print(f"[player] 设置音量失败：{exc}")

    def get_position(self):
        """获取当前播放进度（秒），失败返回 0。"""
        try:
            pos = self._mpv.time_pos
            return pos if pos is not None else 0.0
        except Exception:
            return 0.0

    def get_duration(self):
        """获取当前媒体总时长（秒），失败返回 0。"""
        try:
            dur = self._mpv.duration
            return dur if dur is not None else 0.0
        except Exception:
            return 0.0

    def quit(self):
        """释放 MPV 资源。"""
        try:
            self._mpv.terminate()
        except Exception:
            pass


class BiliResolver:
    """基于 yt-dlp 的 B 站视频解析器。

    注意：本类必须在子线程中调用，否则会阻塞 UI。
    """

    def __init__(self, cookies_file=None):
        self.cookies_file = cookies_file

    def resolve(self, url):
        """解析 B 站链接，返回 yt-dlp info dict。

        返回结果可能是：
            - 单个视频：包含 url / formats 等字段
            - 合集 / 分P / 播放列表：_type == "playlist"，含 entries 列表
        """
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": False,        # 允许展开合集与分 P
            "format": "bestvideo+bestaudio/best",
        }
        # 会员高清：若存在 cookies.txt 则用于请求 1080P/4K
        if self.cookies_file and os.path.exists(self.cookies_file):
            opts["cookiefile"] = self.cookies_file
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:
            raise RuntimeError(f"yt-dlp 解析失败：{exc}") from exc


def extract_playable_list(info):
    """把 yt-dlp 返回的 info 规整为可播放条目列表。

    单视频 → 返回长度为 1 的列表；合集 → 返回所有分 P 的列表。
    """
    if info is None:
        return []
    entries = []
    if info.get("_type") == "playlist" or "entries" in info:
        for entry in info.get("entries") or []:
            if entry:
                entries.append(entry)
    else:
        entries.append(info)
    return entries
