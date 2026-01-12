import base64
import json
import os
import re
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin

from curl_cffi import requests

from nodeseek_login_dual import API_BASE_URL, CLIENT_KEY, solve_turnstile_token

OCR_API_URL = os.getenv("OCR_API_URL", "https://captcha-120546510085.asia-northeast1.run.app")

XSERVER_BASE_URL = os.getenv("XSERVER_BASE_URL", "https://www.xserver.ne.jp")
XSERVER_LOGIN_URL = os.getenv("XSERVER_LOGIN_URL", f"{XSERVER_BASE_URL}/login/")
XSERVER_RENEW_URL = os.getenv("XSERVER_RENEW_URL", f"{XSERVER_BASE_URL}/xserver/renew/")
XSERVER_CAPTCHA_URL = os.getenv("XSERVER_CAPTCHA_URL", "")

XSERVER_TURNSTILE_SITEKEY = os.getenv("XSERVER_TURNSTILE_SITEKEY", "")
XSERVER_RENEW_TURNSTILE_SITEKEY = os.getenv("XSERVER_RENEW_TURNSTILE_SITEKEY", XSERVER_TURNSTILE_SITEKEY)

XSERVER_LOGIN_USER_FIELD = os.getenv("XSERVER_LOGIN_USER_FIELD", "username")
XSERVER_LOGIN_PASS_FIELD = os.getenv("XSERVER_LOGIN_PASS_FIELD", "password")
XSERVER_LOGIN_CAPTCHA_FIELD = os.getenv("XSERVER_LOGIN_CAPTCHA_FIELD", "captcha")
XSERVER_LOGIN_TURNSTILE_FIELD = os.getenv("XSERVER_LOGIN_TURNSTILE_FIELD", "cf-turnstile-response")
XSERVER_RENEW_TURNSTILE_FIELD = os.getenv("XSERVER_RENEW_TURNSTILE_FIELD", "cf-turnstile-response")

XSERVER_LOGIN_METHOD = os.getenv("XSERVER_LOGIN_METHOD", "POST").upper()
XSERVER_RENEW_METHOD = os.getenv("XSERVER_RENEW_METHOD", "POST").upper()

XSERVER_LOGIN_EXTRA_FORM = os.getenv("XSERVER_LOGIN_EXTRA_FORM", "{}")
XSERVER_RENEW_FORM = os.getenv("XSERVER_RENEW_FORM", "{}")

XSERVER_LOGIN_SUCCESS_KEYWORD = os.getenv("XSERVER_LOGIN_SUCCESS_KEYWORD", "")
XSERVER_LOGIN_FAILURE_KEYWORD = os.getenv("XSERVER_LOGIN_FAILURE_KEYWORD", "")
XSERVER_RENEW_SUCCESS_KEYWORD = os.getenv("XSERVER_RENEW_SUCCESS_KEYWORD", "")
XSERVER_RENEW_FAILURE_KEYWORD = os.getenv("XSERVER_RENEW_FAILURE_KEYWORD", "")


def mask(value: Optional[str], keep: int = 4) -> str:
    if not value:
        return "None"
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "..." + value[-keep:]


def load_json_env(raw: str) -> Dict[str, str]:
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except json.JSONDecodeError:
        return {}
    return {}


def get_session():
    try:
        return requests.Session(impersonate="chrome100")
    except requests.exceptions.ImpersonateError:
        return requests.Session(impersonate="chrome99")


def cookie_string_from_session(session: requests.Session) -> str:
    cookies = session.cookies.get_dict()
    return "; ".join([f"{k}={v}" for k, v in cookies.items()])


def extract_hidden_inputs(html: str) -> Dict[str, str]:
    hidden_inputs = {}
    for match in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', html, re.I):
        tag = match.group(0)
        name_match = re.search(r'name=["\']([^"\']+)["\']', tag, re.I)
        value_match = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
        if name_match:
            hidden_inputs[name_match.group(1)] = value_match.group(1) if value_match else ""
    return hidden_inputs


def guess_captcha_url(html: str, base_url: str) -> Optional[str]:
    if XSERVER_CAPTCHA_URL:
        return urljoin(base_url, XSERVER_CAPTCHA_URL)

    candidates = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I)
    for src in candidates:
        if "captcha" in src.lower():
            return urljoin(base_url, src)
    return urljoin(base_url, candidates[0]) if candidates else None


def parse_ocr_response(payload) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("text", "result", "code", "captcha", "answer"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("text", "result", "code", "captcha", "answer"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def solve_ocr(image_bytes: bytes) -> Optional[str]:
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(OCR_API_URL, json={"image": b64}, headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            text = parse_ocr_response(data)
            if text:
                return text
    except Exception:
        pass

    try:
        resp = requests.post(
            OCR_API_URL,
            files={"file": ("captcha.png", image_bytes, "image/png")},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            text = parse_ocr_response(data)
            if text:
                return text
    except Exception:
        return None

    return None


def request_turnstile_token(page_url: str, sitekey: str) -> Optional[str]:
    if not API_BASE_URL or not CLIENT_KEY or not sitekey:
        return None
    return solve_turnstile_token(API_BASE_URL, CLIENT_KEY, page_url, sitekey)


def login_xserver(username: str, password: str) -> Tuple[bool, str, Optional[str]]:
    session = get_session()
    try:
        resp = session.get(XSERVER_LOGIN_URL, timeout=20)
        html = resp.text
    except Exception as exc:
        return False, f"🚫 登录页获取失败: {exc}", None

    captcha_url = guess_captcha_url(html, XSERVER_LOGIN_URL)
    if not captcha_url:
        return False, "🚫 未找到验证码图片地址，请配置 XSERVER_CAPTCHA_URL", None

    try:
        captcha_resp = session.get(captcha_url, timeout=20)
        captcha_image = captcha_resp.content
    except Exception as exc:
        return False, f"🚫 获取验证码图片失败: {exc}", None

    captcha_text = solve_ocr(captcha_image)
    if not captcha_text:
        return False, "🚫 OCR 识别失败，请检查 OCR 服务", None

    turnstile_token = request_turnstile_token(XSERVER_LOGIN_URL, XSERVER_TURNSTILE_SITEKEY)
    if not turnstile_token:
        return False, "🚫 Turnstile 验证失败，请检查 API_BASE_URL/CLIENT_KEY/站点密钥", None

    payload = extract_hidden_inputs(html)
    payload.update(load_json_env(XSERVER_LOGIN_EXTRA_FORM))
    payload.update(
        {
            XSERVER_LOGIN_USER_FIELD: username,
            XSERVER_LOGIN_PASS_FIELD: password,
            XSERVER_LOGIN_CAPTCHA_FIELD: captcha_text,
            XSERVER_LOGIN_TURNSTILE_FIELD: turnstile_token,
        }
    )

    headers = {
        "Referer": XSERVER_LOGIN_URL,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    }

    try:
        if XSERVER_LOGIN_METHOD == "GET":
            login_resp = session.get(XSERVER_LOGIN_URL, params=payload, headers=headers, timeout=30)
        else:
            login_resp = session.post(XSERVER_LOGIN_URL, data=payload, headers=headers, timeout=30)
    except Exception as exc:
        return False, f"🚫 登录请求失败: {exc}", None

    login_text = login_resp.text or ""
    if XSERVER_LOGIN_FAILURE_KEYWORD and XSERVER_LOGIN_FAILURE_KEYWORD in login_text:
        return False, "🚫 登录失败：账号或验证码错误", None
    if XSERVER_LOGIN_SUCCESS_KEYWORD and XSERVER_LOGIN_SUCCESS_KEYWORD not in login_text:
        return False, "🚫 登录失败：未检测到成功标识", None
    if login_resp.status_code >= 400:
        return False, f"🚫 登录失败：HTTP {login_resp.status_code}", None

    return True, "✅ 登录成功", cookie_string_from_session(session)


def renew_xserver(username: str, password: str) -> Dict[str, str]:
    ok, message, cookie = login_xserver(username, password)
    if not ok:
        return {
            "name": username,
            "result": message,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "site_type": "xs",
        }

    session = get_session()
    if cookie:
        for pair in cookie.split(";"):
            if "=" in pair:
                key, value = pair.strip().split("=", 1)
                session.cookies.set(key, value)

    hidden_fields = {}
    try:
        pre_resp = session.get(XSERVER_RENEW_URL, timeout=20)
        hidden_fields = extract_hidden_inputs(pre_resp.text)
    except Exception:
        hidden_fields = {}

    payload = {}
    payload.update(hidden_fields)
    payload.update(load_json_env(XSERVER_RENEW_FORM))

    turnstile_token = request_turnstile_token(XSERVER_RENEW_URL, XSERVER_RENEW_TURNSTILE_SITEKEY)
    if turnstile_token:
        payload[XSERVER_RENEW_TURNSTILE_FIELD] = turnstile_token

    headers = {
        "Referer": XSERVER_RENEW_URL,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    }

    try:
        if XSERVER_RENEW_METHOD == "GET":
            renew_resp = session.get(XSERVER_RENEW_URL, params=payload, headers=headers, timeout=30)
        else:
            renew_resp = session.post(XSERVER_RENEW_URL, data=payload, headers=headers, timeout=30)
    except Exception as exc:
        return {
            "name": username,
            "result": f"🚫 续期请求失败: {exc}",
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "site_type": "xs",
        }

    renew_text = renew_resp.text or ""
    if XSERVER_RENEW_FAILURE_KEYWORD and XSERVER_RENEW_FAILURE_KEYWORD in renew_text:
        result = "🚫 续期失败：返回失败标识"
    elif XSERVER_RENEW_SUCCESS_KEYWORD and XSERVER_RENEW_SUCCESS_KEYWORD not in renew_text:
        result = "🚫 续期失败：未检测到成功标识"
    elif renew_resp.status_code >= 400:
        result = f"🚫 续期失败：HTTP {renew_resp.status_code}"
    else:
        result = "✅ 续期请求已提交"

    return {
        "name": username,
        "result": result,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "site_type": "xs",
        "cookie": cookie or "",
    }
