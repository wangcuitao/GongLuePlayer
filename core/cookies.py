# -*- coding: utf-8 -*-
"""Cookie 辅助模块：手动粘贴并保存 B 站登录 Cookie，以及有效性测试。

只支持手动方式（F12 开发者工具复制 Cookie，无需任何浏览器插件）。
保存为 Netscape 格式的 cookies.txt，供播放器请求 B 站高清画质使用；
测试则用保存的 Cookie 真实请求 B 站接口，确认登录态有效。
"""
import json
import os
import re
import urllib.request

# B 站个人导航接口：已登录返回 isLogin=true，否则 code=-101
_BILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"


def save_cookie_string(raw_text, out_path):
    """把用户粘贴的 Cookie 字符串保存为 Netscape 格式的 cookies.txt。

    支持三种输入（均可）：
        1. 完整 Cookie 头：name=value; name=value; ...（从 F12 复制）
        2. 单独的 SESSDATA 值（形如 xxxxx 的一串 token）
        3. 带 "Cookie:" 前缀的整行

    返回 (ok, message)。
    """
    raw = (raw_text or "").strip()
    if not raw:
        return False, "未输入任何内容"

    # 去掉可能误复制进来的 "Cookie:" 前缀（大小写不敏感）
    raw = re.sub(r"^\s*cookie\s*:\s*", "", raw, flags=re.IGNORECASE).strip()

    # 单个 token（不含 = 和 ;）当作 SESSDATA 值
    if "=" not in raw and ";" not in raw:
        raw = f"SESSDATA={raw}"

    pairs = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k and v:
            pairs.append((k, v))

    if not pairs:
        return False, "无法解析输入，请确认粘贴的是完整 Cookie 内容（形如 name=value; name=value）"

    keys = {k for k, _ in pairs}
    if "SESSDATA" not in keys:
        return False, "粘贴内容里没有 SESSDATA，请确认复制的是 bilibili.com 的完整 Cookie（或只粘贴 SESSDATA 的值）"

    # 统一写到 .bilibili.com 域（覆盖 www/api/bilibili.com 等子域），expiry=0 表示会话 Cookie
    lines = ["# Netscape HTTP Cookie File"]
    for k, v in pairs:
        lines.append(f".bilibili.com\tTRUE\t/\tTRUE\t0\t{k}\t{v}")

    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as exc:
        return False, f"保存失败：{exc}"

    return True, f"已保存 Cookie 至 {out_path}"


def load_cookie_header(cookies_file):
    """从 cookies.txt 解析出可用的 Cookie 请求头字符串；失败返回 None。"""
    if not cookies_file or not os.path.exists(cookies_file):
        return None
    pairs = []
    try:
        with open(cookies_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                name, value = parts[5], parts[6]
                if name and value:
                    pairs.append(f"{name}={value}")
    except Exception:
        return None
    return "; ".join(pairs) if pairs else None


def test_cookie(cookies_file):
    """用保存的 Cookie 真实请求 B 站接口，返回 (ok, message)。"""
    header = load_cookie_header(cookies_file)
    if not header:
        return False, "未找到有效的 cookies.txt"

    req = urllib.request.Request(_BILI_NAV_URL, headers={
        "Cookie": header,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as exc:
        return False, f"测试请求失败：{exc}"

    try:
        code = data.get("code")
        nav = data.get("data") or {}
        is_login = bool(nav.get("isLogin"))
        uname = nav.get("uname") or ""
    except Exception:
        return False, "测试返回内容异常，无法解析"

    if code == 0 and is_login:
        return True, f"Cookie 有效，已登录（{uname}）"
    if code == -101 or not is_login:
        return False, "Cookie 无效或已过期，请重新登录 bilibili.com 后重新复制"
    return False, f"Cookie 未通过验证（code={code}）"
