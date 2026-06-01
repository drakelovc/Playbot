"""Авторизованная сессия Steam: логин через IAuthenticationService +
shared_secret/identity_secret из .maFile. Поддерживает:

* `revoke_all_other_sessions()` — отзыв всех остальных сессий пользователя;
* `change_password(new_password)` — смена пароля через help.steampowered.com
  (wizard recovery), с обязательным mobile-confirm через identity_secret.

Портировано из FunPay-плагина `steam_rental_v2.4.4/steam_rental.py` —
ту же реализацию использует stable build, она протестирована автором
оригинала. Импорты `rsa` и `steampy.confirmation` подгружаются лениво,
чтобы плагин аренды мог импортироваться даже без них.
"""
from __future__ import annotations

import logging
import time
from base64 import b64encode
from typing import Any
from urllib.parse import urlparse, parse_qs

import requests

LOGGER = logging.getLogger("autosteamrental.session")

# ── Steam endpoints ─────────────────────────────────────────────────────────
_API = "https://api.steampowered.com"
_COMMUNITY = "https://steamcommunity.com"
_STORE = "https://store.steampowered.com"
_LOGIN_HOST = "https://login.steampowered.com"
_HELP = "https://help.steampowered.com"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
)


class SteamError(RuntimeError):
    """Любая ошибка взаимодействия со Steam (RSA, 2FA, recovery wizard, …)."""


class SteamSession:
    """Авторизованная сессия Steam, поднимаемая из логина/пароля + .maFile.

    Usage::

        sess = SteamSession(login, password, shared_secret, identity_secret)
        sess.login()
        sess.revoke_all_other_sessions()
        sess.change_password(_gen_password())
    """

    def __init__(self, account_name: str, password: str,
                 shared_secret: str, identity_secret: str,
                 steamid: str | None = None):
        from steampy import guard as steam_guard

        self.account_name = account_name
        self.password = password
        self.shared_secret = shared_secret
        self.identity_secret = identity_secret
        self.steamid: str | None = steamid
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": _USER_AGENT})
        self._guard = steam_guard

    # ── 2FA helpers ─────────────────────────────────────────────────────────
    def generate_2fa_code(self) -> str:
        return self._guard.generate_one_time_code(self.shared_secret)

    # ── login ───────────────────────────────────────────────────────────────
    def login(self) -> None:
        from rsa import PublicKey, encrypt as rsa_encrypt

        rsa_resp = self.sess.get(
            f"{_API}/IAuthenticationService/GetPasswordRSAPublicKey/v1/",
            params={"account_name": self.account_name}, timeout=15)
        rsa_data = rsa_resp.json().get("response", {}) or {}
        if "publickey_mod" not in rsa_data:
            raise SteamError("Не удалось получить RSA-ключ от Steam")
        rsa_key = PublicKey(int(rsa_data["publickey_mod"], 16),
                            int(rsa_data["publickey_exp"], 16))
        enc_pw = b64encode(rsa_encrypt(self.password.encode(), rsa_key)).decode()

        begin_data = {
            "account_name": self.account_name,
            "encrypted_password": enc_pw,
            "encryption_timestamp": rsa_data["timestamp"],
            "persistence": "1",
        }
        client_id = None
        request_id = None
        steam_id = None
        for _attempt in range(3):
            resp = self.sess.post(
                f"{_API}/IAuthenticationService/BeginAuthSessionViaCredentials/v1/",
                data=begin_data, timeout=15)
            rd = resp.json().get("response", {}) or {}
            client_id = rd.get("client_id")
            request_id = rd.get("request_id")
            steam_id = rd.get("steamid")
            if client_id:
                break
            time.sleep(1)
        if not client_id:
            raise SteamError("Steam не вернул client_id (возможно, неверный пароль)")
        steam_id = str(steam_id)

        code = self.generate_2fa_code()
        self.sess.post(
            f"{_API}/IAuthenticationService/UpdateAuthSessionWithSteamGuardCode/v1/",
            data={"client_id": client_id, "steamid": steam_id,
                  "code_type": 3, "code": code}, timeout=15)

        refresh_token = None
        for _ in range(10):
            poll = self.sess.post(
                f"{_API}/IAuthenticationService/PollAuthSessionStatus/v1/",
                data={"client_id": client_id, "request_id": request_id},
                timeout=15)
            refresh_token = poll.json().get("response", {}).get("refresh_token")
            if refresh_token:
                break
            time.sleep(2)
        if not refresh_token:
            raise SteamError("Не удалось получить refresh_token (Steam Guard fail)")

        self.sess.get(_COMMUNITY, timeout=15)
        sessionid = self.sess.cookies.get("sessionid", "")
        fin = self.sess.post(
            f"{_LOGIN_HOST}/jwt/finalizelogin",
            data={"nonce": refresh_token, "sessionid": sessionid,
                  "redir": f"{_COMMUNITY}/login/home/?goto="},
            timeout=15)
        fin_json = fin.json()
        for ti in fin_json.get("transfer_info", []):
            ti["params"]["steamID"] = fin_json.get("steamID", steam_id)
            try:
                self.sess.post(ti["url"], ti["params"], timeout=10)
            except Exception:
                pass

        self.steamid = str(fin_json.get("steamID") or steam_id)
        LOGGER.info("login OK для %s (steamid=%s)",
                    self.account_name, self.steamid)

    # ── helpers ─────────────────────────────────────────────────────────────
    def sessionid_for(self, host: str) -> str:
        try:
            self.sess.get(host, timeout=15)
        except Exception:
            pass
        for cookie in self.sess.cookies:
            if cookie.name == "sessionid":
                if cookie.domain.lstrip(".") in host:
                    return cookie.value
        return self.sess.cookies.get("sessionid", "") or ""

    @staticmethod
    def _safe_json(r: "requests.Response") -> dict[str, Any]:
        try:
            return r.json() or {}
        except Exception:
            return {}

    # ── revoke all other sessions ───────────────────────────────────────────
    def revoke_all_other_sessions(self) -> bool:
        sessionid = self.sessionid_for(_STORE)
        if not sessionid:
            raise SteamError("Нет sessionid для store.steampowered.com")
        endpoints = [
            (f"{_STORE}/twofactor/manage_action",
             {"action": "deauthorize", "sessionid": sessionid}),
            (f"{_COMMUNITY}/profiles/{self.steamid}/edit/info",
             {"sessionID": sessionid, "type": "deauthorize"}),
        ]
        ok = False
        for url, payload in endpoints:
            try:
                resp = self.sess.post(url, data=payload, timeout=15,
                                      headers={"Referer": f"{_STORE}/account/"})
                if resp.status_code < 400:
                    ok = True
            except Exception:
                pass
        return ok

    # ── warmup (anti-dormant) ───────────────────────────────────────────────
    def warmup(self, idle_seconds: int = 30) -> bool:
        """Логин + idle N сек + лёгкий пинг community/store, чтобы Steam
        пометил аккаунт «недавно активен». Используется чтобы аккаунты
        в пуле не уходили в dormant-режим.

        Возвращает True если login прошёл и pings успешны.
        """
        idle_seconds = max(1, min(int(idle_seconds), 600))
        self.login()
        ok = True
        elapsed = 0
        # Лёгкие пинги: пара GET-ов в community + store раз в 10 сек,
        # чтобы сессия выглядела «живой» (просмотры профиля/инвентаря).
        targets = [
            f"{_COMMUNITY}/profiles/{self.steamid}/",
            f"{_STORE}/account/",
            f"{_COMMUNITY}/profiles/{self.steamid}/inventory/",
        ]
        idx = 0
        while elapsed < idle_seconds:
            try:
                r = self.sess.get(targets[idx % len(targets)],
                                  timeout=15, allow_redirects=True)
                if r.status_code >= 400:
                    ok = False
            except Exception:
                ok = False
            idx += 1
            step = min(10, idle_seconds - elapsed)
            time.sleep(step)
            elapsed += step
        return ok

    # ── change password (через wizard recovery) ─────────────────────────────
    def change_password(self, new_password: str) -> None:
        from rsa import PublicKey, encrypt as rsa_encrypt

        sid_help = self.sessionid_for(_HELP)
        if not sid_help:
            raise SteamError(
                "Нет sessionid для help.steampowered.com (логин истёк?)")

        r1 = self.sess.get(
            f"{_HELP}/wizard/HelpChangePassword?redir=store/account/",
            headers={"User-Agent": _USER_AGENT,
                     "Referer": f"{_STORE}/", "Accept": "text/html"},
            allow_redirects=True, timeout=15)
        final_url = r1.url
        qs = parse_qs(urlparse(final_url).query)
        params = {k: qs.get(k, [""])[0] for k in
                  ("s", "account", "reset", "lost", "issueid")}
        if not params["s"]:
            raise SteamError(
                "Не удалось получить параметры wizard-recovery "
                "(нужен валидный логин в Steam)")

        self.sess.get(
            f"{_HELP}/en/wizard/HelpWithLoginInfoEnterCode",
            params={**params, "sessionid": sid_help,
                    "wizard_ajax": 1, "gamepad": 0},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest"}, timeout=15)

        r3 = self.sess.post(
            f"{_HELP}/en/wizard/AjaxSendAccountRecoveryCode",
            data={"sessionid": sid_help, "wizard_ajax": "1", "gamepad": "0",
                  "s": params["s"], "method": "8", "link": "", "n": "1"},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _HELP,
                     "Referer": f"{_HELP}/en/wizard/HelpWithLoginInfoEnterCode"},
            timeout=15)
        r3_json = self._safe_json(r3)
        if r3_json.get("errorMsg"):
            raise SteamError(
                f"AjaxSendAccountRecoveryCode: {r3_json['errorMsg']}")

        self._mobile_confirm_recovery(params["s"])

        self.sess.post(
            f"{_HELP}/en/wizard/AjaxPollAccountRecoveryConfirmation",
            data={"sessionid": sid_help, "wizard_ajax": 1,
                  "s": params["s"], "reset": params["reset"],
                  "lost": params["lost"], "method": 8,
                  "issueid": params["issueid"], "gamepad": 0},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _HELP}, timeout=15)

        self.sess.get(
            f"{_HELP}/en/wizard/AjaxVerifyAccountRecoveryCode",
            params={"code": "", "s": params["s"], "reset": params["reset"],
                    "lost": params["lost"], "method": 8,
                    "issueid": params["issueid"], "sessionid": sid_help,
                    "wizard_ajax": 1, "gamepad": 0},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest"}, timeout=15)

        self.sess.post(
            f"{_HELP}/en/wizard/AjaxAccountRecoveryGetNextStep",
            data={"sessionid": sid_help, "wizard_ajax": 1, "s": params["s"],
                  "account": params["account"], "reset": params["reset"],
                  "issueid": params["issueid"], "lost": 2},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _HELP}, timeout=15)

        def _fetch_rsa() -> tuple["PublicKey", str]:
            rsa_r = self.sess.post(
                f"{_HELP}/en/login/getrsakey/",
                data={"sessionid": sid_help, "username": self.account_name},
                headers={"User-Agent": _USER_AGENT,
                         "X-Requested-With": "XMLHttpRequest",
                         "Origin": _HELP}, timeout=15)
            rsa_json = self._safe_json(rsa_r)
            if "publickey_mod" not in rsa_json:
                raise SteamError("Не удалось получить RSA-ключ Steam")
            return (PublicKey(int(rsa_json["publickey_mod"], 16),
                              int(rsa_json["publickey_exp"], 16)),
                    rsa_json["timestamp"])

        # VerifyPassword: ТЕКУЩИЙ пароль (proof of ownership)
        rsa_key_old, ts_old = _fetch_rsa()
        enc_old = b64encode(rsa_encrypt(self.password.encode("ascii"),
                                         rsa_key_old)).decode()
        vp = self.sess.post(
            f"{_HELP}/en/wizard/AjaxAccountRecoveryVerifyPassword/",
            data={"sessionid": sid_help, "s": params["s"], "lost": 2,
                  "reset": 1, "password": enc_old, "rsatimestamp": ts_old},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _HELP}, timeout=15)
        vp_json = self._safe_json(vp)
        if vp_json.get("errorMsg"):
            raise SteamError(
                f"AjaxAccountRecoveryVerifyPassword: {vp_json['errorMsg']}")

        # CheckPasswordAvailable: новый пароль (plaintext)
        chk = self.sess.post(
            f"{_HELP}/en/wizard/AjaxCheckPasswordAvailable/",
            data={"sessionid": sid_help, "wizard_ajax": 1,
                  "password": new_password},
            headers={"User-Agent": _USER_AGENT, "Origin": _HELP}, timeout=15)
        chk_json = self._safe_json(chk)
        if not chk_json.get("available", True):
            raise SteamError(
                "Steam: новый пароль недоступен (слишком простой/похожий)")

        # ChangePassword: НОВЫЙ пароль (со свежим RSA timestamp)
        rsa_key_new, ts_new = _fetch_rsa()
        enc_new = b64encode(rsa_encrypt(new_password.encode("ascii"),
                                         rsa_key_new)).decode()
        ch = self.sess.post(
            f"{_HELP}/en/wizard/AjaxAccountRecoveryChangePassword/",
            data={"sessionid": sid_help, "wizard_ajax": 1, "s": params["s"],
                  "account": params["account"], "password": enc_new,
                  "rsatimestamp": ts_new},
            headers={"User-Agent": _USER_AGENT,
                     "X-Requested-With": "XMLHttpRequest",
                     "Origin": _HELP}, timeout=15)
        ch_json = self._safe_json(ch)
        if ch_json.get("errorMsg"):
            raise SteamError(
                f"AjaxAccountRecoveryChangePassword: {ch_json['errorMsg']}")

        self.password = new_password
        LOGGER.info("пароль успешно изменён для %s", self.account_name)

    # ── mobile confirm via .maFile ──────────────────────────────────────────
    def _mobile_confirm_recovery(self, s_id: str) -> None:
        from steampy.confirmation import ConfirmationExecutor

        if not self.steamid:
            raise SteamError("Нет steamid — нужно сначала залогиниться")

        ce = ConfirmationExecutor(self.identity_secret, self.steamid, self.sess)
        last_exc: Exception | None = None
        for _try in range(6):
            try:
                confs = ce._get_confirmations()
            except Exception as exc:
                last_exc = exc
                time.sleep(2)
                continue
            target = None
            for c in confs:
                cid = (getattr(c, "data_accept", None)
                       or getattr(c, "creator_id", None)
                       or getattr(c, "creator", None))
                if cid and str(cid) == str(s_id):
                    target = c
                    break
            if target is None and confs:
                target = confs[-1]
            if target is not None:
                try:
                    ce._send_confirmation(target)
                    return
                except Exception as exc:
                    last_exc = exc
                    time.sleep(2)
            else:
                time.sleep(2)
        raise SteamError(
            f"Не удалось подтвердить запрос смены пароля через mobile "
            f"({type(last_exc).__name__ if last_exc else 'нет confirmation'}: "
            f"{last_exc or ''})")


__all__ = ["SteamError", "SteamSession"]
