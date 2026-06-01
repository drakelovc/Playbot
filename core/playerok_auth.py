"""
Авторизация на Playerok по email: отправка кода и подтверждение.

Flow:
  1. send_sign_in_code(email, proxy) → Playerok шлёт код на почту
  2. confirm_sign_in_code(email, code, proxy) → Playerok возвращает token + cookies
  3. Бот сохраняет cookies в конфиг и подключается
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile

import curl_cffi

log = logging.getLogger("playerok_bot.auth")

BASE_URL = "https://playerok.com"
GRAPHQL_URL = f"{BASE_URL}/graphql"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)

# ── GraphQL mutations для авторизации ──────────────────────────────────

SIGN_IN_SEND_CODE = """
mutation signInSendCode($input: SignInSendCodeInput!) {
  signInSendCode(input: $input) {
    ... on SignInSendCodeSuccessResult {
      expiresAt
      __typename
    }
    ... on SignInSendCodeFailureResult {
      reason
      __typename
    }
    __typename
  }
}
""".strip()

SIGN_IN_CONFIRM_CODE = """
mutation signInConfirmCode($input: SignInConfirmCodeInput!) {
  signInConfirmCode(input: $input) {
    ... on SignInConfirmCodeSuccessResult {
      token
      expiresAt
      __typename
    }
    ... on SignInConfirmCodeFailureResult {
      reason
      attemptsLeft
      __typename
    }
    __typename
  }
}
""".strip()


def _build_headers(user_agent: str = "") -> dict:
    return {
        "accept": "*/*",
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "apollo-require-preflight": "true",
        "apollographql-client-name": "web",
        "content-type": "application/json",
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/",
        "user-agent": user_agent or DEFAULT_UA,
    }


def _make_session(proxy: str | None = None):
    cert_src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "playerokapi", "cacert.pem")
    cert_dst = os.path.join(tempfile.gettempdir(), "playerok_auth_cacert.pem")
    if os.path.exists(cert_src):
        shutil.copyfile(cert_src, cert_dst)

    proxy_url = None
    if proxy:
        p = proxy.replace("https://", "").replace("http://", "").strip()
        if p:
            proxy_url = f"http://{p}"

    session = curl_cffi.Session(
        impersonate="chrome",
        timeout=20,
        proxy=proxy_url,
        verify=cert_dst if os.path.exists(cert_dst) else True,
    )
    return session


def send_sign_in_code(
    email: str,
    proxy: str | None = None,
    user_agent: str = "",
) -> dict:
    """
    Отправляет код подтверждения на email.

    Returns:
        {"ok": True, "expires_at": "..."} при успехе
        {"ok": False, "error": "reason"} при ошибке
    """
    session = _make_session(proxy)
    headers = _build_headers(user_agent)

    payload = {
        "operationName": "signInSendCode",
        "variables": {
            "input": {
                "email": email.strip().lower(),
            }
        },
        "query": SIGN_IN_SEND_CODE,
    }

    try:
        resp = session.post(
            GRAPHQL_URL,
            headers=headers,
            data=json.dumps(payload),
        )

        if resp.status_code != 200:
            log.warning("signInSendCode HTTP %s: %s", resp.status_code, resp.text[:300])
            return {"ok": False, "error": f"HTTP {resp.status_code}"}

        data = resp.json()
        errors = data.get("errors")
        if errors:
            msg = errors[0].get("message", str(errors))
            log.warning("signInSendCode GQL error: %s", msg)
            return {"ok": False, "error": msg}

        result = (data.get("data") or {}).get("signInSendCode") or {}
        typename = result.get("__typename", "")

        if "Success" in typename:
            return {"ok": True, "expires_at": result.get("expiresAt")}

        reason = result.get("reason", "Неизвестная ошибка")
        return {"ok": False, "error": reason}

    except Exception as exc:
        log.exception("signInSendCode exception")
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            session.close()
        except Exception:
            pass


def confirm_sign_in_code(
    email: str,
    code: str,
    proxy: str | None = None,
    user_agent: str = "",
) -> dict:
    """
    Подтверждает код и получает token/cookies.

    Returns:
        {"ok": True, "token": "...", "cookies": "token=...;__ddg5_=...", "raw_cookies": {...}}
        {"ok": False, "error": "...", "attempts_left": N}
    """
    session = _make_session(proxy)
    headers = _build_headers(user_agent)

    payload = {
        "operationName": "signInConfirmCode",
        "variables": {
            "input": {
                "email": email.strip().lower(),
                "code": code.strip(),
            }
        },
        "query": SIGN_IN_CONFIRM_CODE,
    }

    try:
        resp = session.post(
            GRAPHQL_URL,
            headers=headers,
            data=json.dumps(payload),
        )

        if resp.status_code != 200:
            log.warning("signInConfirmCode HTTP %s: %s", resp.status_code, resp.text[:300])
            return {"ok": False, "error": f"HTTP {resp.status_code}"}

        # Собираем cookies из ответа (Set-Cookie)
        raw_cookies: dict[str, str] = {}
        set_cookie_headers = []
        if hasattr(resp, 'headers'):
            for key, val in resp.headers.items():
                if key.lower() == "set-cookie":
                    set_cookie_headers.append(val)

        for sc in set_cookie_headers:
            parts = sc.split(";")[0].strip()
            if "=" in parts:
                k, v = parts.split("=", 1)
                raw_cookies[k.strip()] = v.strip()

        data = resp.json()
        errors = data.get("errors")
        if errors:
            msg = errors[0].get("message", str(errors))
            log.warning("signInConfirmCode GQL error: %s", msg)
            return {"ok": False, "error": msg}

        result = (data.get("data") or {}).get("signInConfirmCode") or {}
        typename = result.get("__typename", "")

        if "Success" in typename:
            token = result.get("token", "")
            if token:
                raw_cookies["token"] = token

            cookies_str = "; ".join(f"{k}={v}" for k, v in raw_cookies.items())

            return {
                "ok": True,
                "token": token,
                "cookies": cookies_str,
                "raw_cookies": raw_cookies,
            }

        reason = result.get("reason", "Неверный код или ошибка")
        attempts = result.get("attemptsLeft")
        return {"ok": False, "error": reason, "attempts_left": attempts}

    except Exception as exc:
        log.exception("signInConfirmCode exception")
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            session.close()
        except Exception:
            pass
