#!/usr/bin/env python3
"""HAUT Campus Network Auto Login. Python 3.9+, standard library only."""

import argparse
import base64
import ctypes
import getpass
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field, fields
from http.client import HTTPException


LOG = logging.getLogger("srun")
BASE64_ALPHABET = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"
STANDARD_BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
MASK = 0xFFFFFFFF
DEFAULT_CONFIG = Path(__file__).resolve().with_name("config.json")


class SrunError(Exception):
    """An error that is safe to show without exposing request URLs/secrets."""


class ConfigError(SrunError):
    pass


class ProtocolError(SrunError, ValueError):
    pass


class NetworkError(SrunError):
    pass


class AuthenticationError(SrunError):
    """A server-side refusal: stop automatic retries until reviewed."""


def _js_words(value, include_length=False):
    # Match the portal's charCodeAt/bitwise operations, including non-ASCII.
    raw = value.encode("utf-16-le", errors="surrogatepass")
    units = struct.unpack("<" + "H" * (len(raw) // 2), raw)
    words = []
    for offset in range(0, len(units), 4):
        word = 0
        for index, unit in enumerate(units[offset:offset + 4]):
            word |= unit << (8 * index)
        words.append(word & MASK)
    if include_length:
        words.append(len(units))
    return words


def xencode(message: str, key: str) -> bytes:
    """Portal variant of XXTEA; its mix expression differs from standard XXTEA."""
    if not message:
        return b""
    value = _js_words(message, True)
    key_words = _js_words(key)
    key_words += [0] * max(0, 4 - len(key_words))
    last = len(value) - 1
    z = value[last]
    total = 0
    for _ in range(6 + 52 // len(value)):
        total = (total + 0x9E3779B9) & MASK
        e = (total >> 2) & 3
        for position in range(last + 1):
            y = value[position + 1] if position < last else value[0]
            mix = (z >> 5) ^ ((y << 2) & MASK)
            mix += ((y >> 3) ^ ((z << 4) & MASK)) ^ (total ^ y)
            mix += key_words[(position & 3) ^ e] ^ z
            value[position] = (value[position] + mix) & MASK
            z = value[position]
    return struct.pack("<" + "I" * len(value), *value)


def password_digest(password: str, token: str) -> str:
    return hmac.new(token.encode("utf-8"), password.encode("utf-8"), hashlib.md5).hexdigest()


def encode_info(username: str, password: str, ip: str, ac_id: str, token: str) -> str:
    plain = json.dumps({
        "username": username, "password": password, "ip": ip,
        "acid": str(ac_id), "enc_ver": "srun_bx1",
    }, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.b64encode(xencode(plain, token)).decode("ascii")
    return "{SRBX1}" + encoded.translate(str.maketrans(STANDARD_BASE64, BASE64_ALPHABET))


def checksum(username: str, hmd5: str, ac_id: str, ip: str, info: str, token: str) -> str:
    values = (username, hmd5, str(ac_id), ip, "200", "1", info)
    return hashlib.sha1("".join(token + value for value in values).encode("utf-8")).hexdigest()


def build_login_params(username: str, password: str, ip: str, ac_id: str, token: str) -> dict:
    digest = password_digest(password, token)
    info = encode_info(username, password, ip, ac_id, token)
    return {
        "action": "login", "username": username, "password": "{MD5}" + digest,
        "ac_id": str(ac_id), "ip": ip,
        "chksum": checksum(username, digest, ac_id, ip, info, token),
        "info": info, "n": "200", "type": "1", "os": "Windows 10",
        "name": "Windows", "double_stack": "0",
    }


def parse_jsonp(text: str, callback=None) -> dict:
    """Parse JSON or an exact JSONP wrapper; never evaluate JavaScript."""
    text = text.lstrip("\ufeff").strip()
    if text.startswith("{"):
        body = text
    else:
        match = re.fullmatch(r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\((.*)\)\s*;?", text, re.DOTALL)
        if not match or (callback is not None and match.group(1) != callback):
            raise ProtocolError("认证服务器返回了无效的 JSONP。")
        body = match.group(2)
    try:
        data = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("认证服务器返回了无效的 JSON。") from exc
    if not isinstance(data, dict):
        raise ProtocolError("认证服务器返回的数据不是 JSON 对象。")
    return data


@dataclass
class Config:
    server: str = "http://172.16.154.130"
    username: str = ""
    ac_id: str = "1"
    ip: str = ""
    timeout: float = 10
    interval: float = 60
    retry_max_interval: float = 900
    password: str = field(default="", repr=False)
    password_dpapi: str = field(default="", repr=False)

    @classmethod
    def from_mapping(cls, data):
        if not isinstance(data, dict):
            raise ConfigError("配置文件必须是 JSON 对象。")
        unknown = set(data) - {field.name for field in fields(cls)}
        if unknown:
            raise ConfigError("配置文件含有未知字段，请对照 config.example.json 检查。")
        config = cls(**data)
        for name in ("server", "username", "ip", "password", "password_dpapi"):
            if not isinstance(getattr(config, name), str):
                raise ConfigError("配置中的 %s 必须是字符串。" % name)
        config.username = config.username.strip()
        config.server = config.server.rstrip("/")
        try:
            parsed = urllib.parse.urlsplit(config.server)
            _ = parsed.port  # Validate an optional port as well as the hostname.
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment):
                raise ValueError
        except ValueError as exc:
            raise ConfigError("server 必须是 http(s)://主机[:端口]，不能含路径或凭据。") from exc
        if isinstance(config.ac_id, bool) or not str(config.ac_id).isdigit() or int(config.ac_id) <= 0:
            raise ConfigError("ac_id 必须是正整数或相应字符串。")
        config.ac_id = str(config.ac_id)
        for name in ("timeout", "interval", "retry_max_interval"):
            value = getattr(config, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ConfigError("%s 必须是有限的正数。" % name)
        if config.interval < 10:
            raise ConfigError("interval 至少为 10 秒，避免频繁请求认证服务器。")
        if config.retry_max_interval < config.interval:
            raise ConfigError("retry_max_interval 不能小于 interval。")
        if config.ip:
            validate_ip(config.ip)
        return config


def validate_ip(value):
    try:
        address = ipaddress.ip_address(value)
        if address.is_unspecified:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ProtocolError("未得到有效的客户端 IP；请确认连接校园网，必要时在配置中填写 ip。") from exc
    return str(address)


def load_config(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return Config.from_mapping({}), {}
    except (OSError, ValueError) as exc:
        raise ConfigError("无法读取配置文件，请检查路径和 JSON 格式。") from exc
    return Config.from_mapping(data), data


def _dpapi(raw, decrypt=False):
    if os.name != "nt":
        raise ConfigError("加密保存密码只支持 Windows；请改用 SRUN_PASSWORD 环境变量或交互输入。")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(raw)
    incoming = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ConfigError("Windows 密码加密/解密失败；请在当前 Windows 账号下重新运行 --setup。")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))


def resolve_username(config):
    value = (os.environ.get("SRUN_USERNAME") or config.username).strip()
    if value:
        return value
    if not sys.stdin.isatty():
        raise ConfigError("未配置学号：请先运行 --setup，或设置 SRUN_USERNAME 环境变量。")
    value = input("学号：").strip()
    if not value:
        raise ConfigError("学号不能为空。")
    return value


def resolve_password(config):
    value = os.environ.get("SRUN_PASSWORD")
    if value:
        return value
    if config.password_dpapi:
        try:
            return _dpapi(base64.b64decode(config.password_dpapi, validate=True), True).decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise ConfigError("已保存密码的格式无效，请重新运行 --setup。") from exc
    if config.password:
        return config.password
    if not sys.stdin.isatty():
        raise ConfigError("未配置密码：请先在终端运行 --setup，或设置 SRUN_PASSWORD 环境变量。")
    value = getpass.getpass("校园网密码（输入不显示）：")
    if not value:
        raise ConfigError("密码不能为空。")
    return value


def setup_config(path, config, original):
    if os.name != "nt":
        raise ConfigError("--setup 加密保存密码只支持 Windows；请使用环境变量或交互登录。")
    if not sys.stdin.isatty():
        raise ConfigError("请在交互式终端运行 --setup。")
    username = input("学号 [%s]：" % config.username).strip() or config.username
    if not username:
        raise ConfigError("学号不能为空。")
    password = getpass.getpass("校园网密码（输入不显示）：")
    if not password:
        raise ConfigError("密码不能为空。")
    if password != getpass.getpass("再次输入密码："):
        raise ConfigError("两次输入的密码不一致，未保存。")
    data = dict(original)
    data.update(server=config.server, username=username, ac_id=config.ac_id, ip=config.ip,
                timeout=config.timeout, interval=config.interval, retry_max_interval=config.retry_max_interval,
                password_dpapi=base64.b64encode(_dpapi(password.encode("utf-8"))).decode("ascii"))
    data.pop("password", None)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError as exc:
        raise ConfigError("保存配置失败，请检查目录是否存在且可写。") from exc
    finally:
        temporary.unlink(missing_ok=True)
    LOG.info("账号和加密密码已保存；现在可以运行登录或 --watch。")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # A portal redirect must not forward a credential-bearing GET to another host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class OnlineStatus:
    online: bool
    username: str = ""
    ip: str = ""


class SrunClient:
    def __init__(self, config):
        self.config = config
        # The gateway is on the LAN; bypass system proxy settings.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, endpoint, parameters=None):
        callback = "srun_" + uuid.uuid4().hex
        query = dict(parameters or {})
        query.update(callback=callback, _=str(time.time_ns() // 1_000_000))
        url = self.config.server + endpoint + "?" + urllib.parse.urlencode(query)
        referer = self.config.server + "/srun_portal_pc?" + urllib.parse.urlencode({
            "ac_id": self.config.ac_id, "srun_wait": "1", "theme": "gongda",
        })
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SrunAutoLogin/1.0",
            "Referer": referer, "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01", "Cookie": "lang=zh-CN",
        })
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                raw = response.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise ProtocolError("认证响应过大，请检查 server 配置。")
                text = raw.decode("utf-8-sig")
        except urllib.error.HTTPError as exc:
            raise NetworkError("认证接口 HTTP %d；请检查校园网连接和 server 配置。" % exc.code) from None
        except (urllib.error.URLError, OSError, HTTPException, socket.timeout):
            # urllib exceptions may contain the full password-bearing request URL.
            raise NetworkError("无法连接认证服务器或请求超时，请确认已连接校园网。") from None
        except UnicodeError:
            raise ProtocolError("认证响应不是 UTF-8 JSON，请检查 server 配置。") from None
        return parse_jsonp(text, callback)

    def status(self):
        result = self.request("/cgi-bin/rad_user_info")
        if result.get("error") == "not_online_error":
            return OnlineStatus(False)
        if result.get("error") != "ok":
            raise ProtocolError("无法识别在线状态；可用 --force-login 单独尝试一次登录。")
        username = result.get("user_name") or result.get("username") or ""
        ip = result.get("online_ip") or result.get("client_ip") or ""
        if not isinstance(username, str) or not isinstance(ip, str):
            raise ProtocolError("在线状态字段格式不正确。")
        return OnlineStatus(True, username, ip)

    def check_account(self, status):
        if status.username and status.username != self.config.username:
            raise AuthenticationError("当前 IP 已由其他账号登录；已停止，请先在认证页面确认账号。")

    def login(self, password):
        # No cached challenges or historical IPs: each attempt starts here.
        challenge = self.request("/cgi-bin/get_challenge", {
            "username": self.config.username, "ip": self.config.ip,
        })
        token = challenge.get("challenge")
        if challenge.get("error") != "ok" or not isinstance(token, str) or not token:
            raise ProtocolError("获取 challenge 失败，请检查校园网连接和账号配置。")
        ip = validate_ip(self.config.ip or challenge.get("client_ip") or challenge.get("online_ip"))
        result = self.request("/cgi-bin/srun_portal", build_login_params(
            self.config.username, password, ip, self.config.ac_id, token,
        ))
        error = result.get("error")
        message = result.get("suc_msg")
        if (error == "ok" and message in ("login_ok", "ip_already_online_error")
                and result.get("ecode", 0) in (0, "0")):
            if message == "ip_already_online_error":
                status = self.status()
                if not status.online:
                    raise ProtocolError("服务器提示已在线，但状态接口显示离线，请稍后重试。")
                self.check_account(status)
            return "登录成功。" if message == "login_ok" else "当前账号已经在线。"
        if error == "ok" and message is None and result.get("ecode", 0) in (0, "0"):
            status = self.status()
            if status.online:
                self.check_account(status)
                return "已确认在线。"
            raise ProtocolError("认证响应不含成功标志，且尚未确认在线。")
        # Deliberately do not echo arbitrary server text: some versions reflect input.
        code = result.get("ecode")
        safe_code = str(code) if isinstance(code, (str, int)) and re.fullmatch(r"(?:E\d{1,8}|-?\d{1,8})", str(code)) else ""
        if safe_code in ("", "0"):
            for key in ("error", "error_msg", "ploy_msg"):
                value = result.get(key)
                match = re.search(r"\bE\d{4}\b", value) if isinstance(value, str) else None
                if match:
                    safe_code = match.group(0)
                    break
        suffix = "（错误码 %s）" % safe_code if safe_code else ""
        raise AuthenticationError("认证被拒绝%s；请检查账号、密码、套餐后重新运行。" % suffix)

    def ensure_online(self, password):
        status = self.status()
        if status.online:
            self.check_account(status)
            return "当前账号已经在线。" if status.username else "当前 IP 已经在线（网关未返回账号）。"
        return self.login(password)


def run_watch(client, password, sleep=time.sleep):
    retry_delay = client.config.interval
    previous = None
    while True:
        try:
            message = client.ensure_online(password)
            if message != previous:
                LOG.info(message)
            previous = message
            retry_delay = client.config.interval
            sleep(client.config.interval)
        except (NetworkError, ProtocolError) as exc:
            LOG.warning("%s %.0f 秒后重试。", exc, retry_delay)
            previous = None
            sleep(retry_delay)
            retry_delay = min(retry_delay * 2, client.config.retry_max_interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description="河南工业大学校园网自动认证（Python 标准库，无需安装依赖）")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径，默认脚本目录下 config.json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--setup", action="store_true", help="交互配置账号并用 Windows DPAPI 加密保存密码")
    modes.add_argument("--watch", action="store_true", help="持续检查在线状态，掉线后重连")
    modes.add_argument("--status", action="store_true", help="只查询状态，不需要密码；离线退出码为 1")
    modes.add_argument("--force-login", action="store_true", help="跳过状态检查，直接尝试一次登录")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    try:
        config, original = load_config(args.config)
        if args.setup:
            setup_config(args.config, config, original)
            return 0
        client = SrunClient(config)
        if args.status:
            status = client.status()
            if status.online:
                if config.username:
                    client.check_account(status)
                LOG.info("当前 IP 在线。")
                return 0
            LOG.info("当前 IP 离线。")
            return 1
        # Read credentials once; an unattended watch must never block on a later prompt.
        config.username = resolve_username(config)
        password = resolve_password(config)
        if args.watch:
            LOG.info("开始监测，每 %.0f 秒检查一次；按 Ctrl+C 停止。", config.interval)
            run_watch(client, password)
        else:
            LOG.info(client.login(password) if args.force_login else client.ensure_online(password))
        return 0
    except (ConfigError, AuthenticationError) as exc:
        LOG.error("%s", exc)
        return 2
    except (NetworkError, ProtocolError) as exc:
        LOG.error("%s", exc)
        return 3
    except (EOFError, KeyboardInterrupt):
        LOG.info("已停止。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
