# -*- coding: utf-8 -*-
"""播放器模块：封装 MPV 播放内核，以及基于 yt-dlp 的 B 站视频流解析。

本模块职责：
    1. 在 import mpv 之前，将 libmpv-2.dll 所在目录加入 DLL 搜索路径；
    2. 提供 Player 类，封装 MPV 的播放 / 暂停 / 快进 / 音量等操作；
    3. 提供 BiliResolver 类，使用 yt-dlp 在子线程中解析 B 站真实流地址，
       并自动识别合集（多 P / 播放列表）。
"""
import os
import re
import sys
import tempfile
import urllib.request


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

# 本进程创建的所有音轨临时文件（用于退出时统一兜底清理）
_TEMP_AUDIO_FILES = []


def download_audio_to_temp(audio_url, headers):
    """把 B 站 DASH 音频流下载到本地临时文件，返回路径；失败返回 None。

    用本地文件做 audio-add 可彻底规避远程流在 mpv 里的挂载问题
    （防盗链头、URL 百分号编码、CDN 时效等不确定因素）。
    """
    try:
        referer = headers.get("Referer") or "https://www.bilibili.com"
        user_agent = headers.get("User-Agent") or "Mozilla/5.0"
        req = urllib.request.Request(audio_url, headers={
            "Referer": referer,
            "User-Agent": user_agent,
        })
        fd, path = tempfile.mkstemp(suffix=".m4s", prefix="gonglue_audio_")
        os.close(fd)
        with urllib.request.urlopen(req, timeout=30) as resp, open(path, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        _TEMP_AUDIO_FILES.append(path)
        return path
    except Exception as exc:
        print(f"[player] 下载音轨失败：{exc}")
        return None


def prepare_entry_audio(entry):
    """在后台线程调用：若条目含分离的音频流，则下载到本地临时文件。

    结果写入 entry["_audio_local_path"]。play_entry 会优先用本地路径挂音轨；
    下载失败则为 None，play_entry 会回退到远程 URL（audio-add）。
    """
    try:
        _video_url, audio_url, headers = Player._extract_streams(entry)
        if not audio_url:
            print("[player] 未分离出独立音轨（可能已是合并流，无需挂载）")
            entry["_audio_local_path"] = None
            return
        print(f"[player] 分离出音轨 URL：{audio_url[:70]}...")
        # 已是本地文件则直接沿用
        if not re.match(r"https?://", audio_url):
            entry["_audio_local_path"] = audio_url
            return
        path = download_audio_to_temp(audio_url, headers)
        entry["_audio_local_path"] = path
        if path:
            print(f"[player] 音轨已下载到本地：{path}")
        else:
            print("[player] 音轨下载失败，将回退到远程 URL")
    except Exception as exc:
        print(f"[player] 预处理音轨失败：{exc}")
        entry["_audio_local_path"] = None


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
        self._pending_audio = None   # 待挂载的外部音轨 URL/本地路径（B 站 DASH 分离流）
        self._audio_attached = False # 当前条目音轨是否已挂载完成
        self._audio_tmp_path = None  # 最近一次下载的本地音轨临时文件路径
        self._audio_garbage = []     # 待清理的旧音轨临时文件（延迟删除）
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
                volume_max=150.0,              # 音量上限拉到 150，与音量面板一致
            )
        except Exception as exc:
            raise RuntimeError(f"MPV 初始化失败：{exc}")

        try:
            self._mpv.register_event_callback(self._on_mpv_event)
        except Exception as exc:
            print(f"[player] 注册事件回调失败：{exc}")

    # ------------------------------------------------------------------
    # 事件回调
    # ------------------------------------------------------------------
    def _on_mpv_event(self, event):
        """处理 mpv 底层事件，转发为简化的事件名。"""
        try:
            if event.event_id == mpv.MpvEventID.END_FILE:
                if self.on_event:
                    self.on_event("end_file")
        except Exception:
            pass

    def attach_pending_audio(self):
        """尝试把待挂载的外部音轨用 audio-add 加入并选中（主线程调用）。

        返回 True 表示无需再重试（已挂载 / 无待挂载），False 表示本次失败、
        需稍后重试（保留 _pending_audio）。audio-file 是「仅加载时读取」的选项，
        无法在运行时用属性设置；audio-add 是官方推荐的运行时挂载命令。
        """
        if getattr(self, "_audio_attached", False):
            return True
        audio_url = getattr(self, "_pending_audio", None)
        if not audio_url:
            return True
        try:
            self._mpv.command("audio-add", audio_url, "select")
        except Exception as exc:
            print(f"[player] 挂载音轨失败：{exc}")
            return False
        # 验证是否真的出现了音轨（避免命令「成功」但实际没加载的情况）
        try:
            tracks = self._mpv.track_list or []
            aids = [t for t in tracks if t.get("type") == "audio"]
            info = [{"id": t.get("id"), "title": t.get("title"),
                     "selected": t.get("selected"), "external": t.get("external")}
                    for t in aids]
            print(f"[player] audio-add 后音轨列表：{info}")
            if aids:
                self._pending_audio = None
                self._audio_attached = True
                print("[player] 音轨挂载成功")
                return True
            # 还没有音轨 → 文件可能尚未加载完成，保留待重试
            print("[player] 音轨尚未出现在列表，稍后重试")
            return False
        except Exception as exc:
            print(f"[player] 读取音轨列表失败：{exc}")
            return False

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

        # 优先用已下载的本地音轨（更可靠），否则回退到远程 URL
        local_audio = entry.get("_audio_local_path")
        if local_audio:
            audio_url = local_audio
        # 上一次的本地音轨临时文件不再需要：先记入待清理队列（延迟到切换完成、
        # mpv 释放句柄后再删），避免 Windows 上「文件被占用」导致删不掉而堆积
        prev = getattr(self, "_audio_tmp_path", None)
        if prev and prev != audio_url:
            garbage = getattr(self, "_audio_garbage", [])
            garbage.append(prev)
            self._audio_garbage = garbage
            self._audio_tmp_path = None
        if local_audio:
            self._audio_tmp_path = local_audio

        # 防盗链：B 站流地址必须携带 Referer / User-Agent
        referer = headers.get("Referer") or "https://www.bilibili.com"
        user_agent = headers.get("User-Agent") or ""
        try:
            self._mpv["referrer"] = referer
            if user_agent:
                self._mpv["user-agent"] = user_agent
            self._mpv["http-header-fields"] = ",".join(
                [f"Referer: {referer}"] +
                ([f"User-Agent: {user_agent}"] if user_agent else []))
        except Exception as exc:
            print(f"[player] 设置请求头失败：{exc}")

        # DASH 分离流：先记下外部音轨 URL，随后由主线程轮询调用 attach_pending_audio()
        # 用 audio-add 挂载。audio-file 是「仅加载时读取」的选项，无法通过 options/
        # 属性在运行时设置（会报 -8 property does not exist）。
        self._pending_audio = audio_url or None
        self._audio_attached = False
        print(f"[player] 视频流：{str(video_url)[:70]}...")
        print(f"[player] 音频流：{('本地 ' if local_audio else '')}{(audio_url or '无')[:70]}...")

        try:
            self._mpv.play(video_url)
        except Exception as exc:
            raise RuntimeError(f"播放失败：{exc}") from exc

        # 显式取消暂停，确保进入播放态
        try:
            self._mpv.pause = False
        except Exception:
            pass

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
            # volume 是 double 属性，必须传浮点，否则 mpv 报 -9 格式错误
            self._mpv.volume = float(max(0.0, min(150.0, volume + delta)))
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
            # volume 是 double 属性，必须传浮点，否则 mpv 报 -9 格式错误
            self._mpv.volume = float(max(0.0, min(150.0, value)))
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
        """释放 MPV 资源，并清理本地音轨临时文件。"""
        try:
            self._mpv.terminate()
        except Exception:
            pass
        # terminate 之后 mpv 已释放句柄，此时清理最稳
        self.cleanup_audio_temp()
        # 兜底：删除本进程创建的所有音轨临时文件
        for p in list(_TEMP_AUDIO_FILES):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        _TEMP_AUDIO_FILES.clear()

    def cleanup_audio_temp(self):
        """清理待删除的旧音轨临时文件（主线程调用；失败静默，可能仍被占用）。"""
        garbage = getattr(self, "_audio_garbage", [])
        self._audio_garbage = []
        for p in garbage:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
                    print(f"[player] 已清理临时音轨：{os.path.basename(p)}")
            except Exception:
                # 仍被占用则放回，下次再试（退出时由 quit 兜底）
                self._audio_garbage.append(p)


class BiliResolver:
    """基于 yt-dlp 的 B 站视频解析器。

    注意：本类必须在子线程中调用，否则会阻塞 UI。
    """

    def __init__(self, cookies_file=None):
        self.cookies_file = cookies_file

    def resolve(self, url, flat=False):
        """解析 B 站链接，返回 yt-dlp info dict。

        flat=True：合集只做扁平解析（仅拿标题 / 分 P 链接，不逐个抓流地址），
        用于快速拿到选集列表；单视频不受影响，仍返回完整信息。

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
            "socket_timeout": 15,       # 避免网络卡住时长时间无响应
            "retries": 2,
            "extractor_retries": 1,
        }
        if flat:
            opts["extract_flat"] = "in_playlist"
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
