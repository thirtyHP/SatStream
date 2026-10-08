"""Credential handling for SatStream.

Two credential families are supported:

* Copernicus Data Space Ecosystem (CDSE) -- OAuth2 password grant against
  the CDSE Keycloak. Access tokens last ~10 minutes and are refreshed
  automatically via the refresh token (or a fresh login).
  Register at https://dataspace.copernicus.eu (free).

* USGS EROS / M2M -- username + *application token* (generated in your
  EROS profile, not your login password). Exchanged for an API key that
  is valid for ~2 hours. Register at https://ers.cr.usgs.gov and request
  M2M access.
"""

import datetime

import requests

CDSE_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)
USGS_LOGIN_TOKEN_URL = (
    "https://m2m.cr.usgs.gov/api/api/json/stable/login-token"
)
USER_AGENT = "SatStream-QGIS-Plugin/0.1"


class AuthError(RuntimeError):
    pass


class CopernicusAuth:
    """OAuth2 token manager for the Copernicus Data Space Ecosystem."""

    def __init__(self):
        self.username = ""
        self.password = ""
        self._access_token = None
        self._refresh_token = None
        self._expiry = None

    def configured(self):
        return bool(self.username and self.password)

    def connected(self):
        return (
            self._access_token is not None
            and self._expiry is not None
            and datetime.datetime.utcnow() < self._expiry
        )

    def login(self):
        self._grant({
            "client_id": "cdse-public",
            "grant_type": "password",
            "username": self.username,
            "password": self.password,
        })

    def get_token(self):
        """Return a valid access token, refreshing/logging in as needed."""
        if not self.configured():
            raise AuthError("Copernicus credentials are not set.")
        if self.connected():
            return self._access_token
        if self._refresh_token:
            try:
                self._grant({
                    "client_id": "cdse-public",
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                })
                return self._access_token
            except AuthError:
                pass  #refresh token expired, fall through to login
        self.login()
        return self._access_token

    def _grant(self, data):
        try:
            resp = requests.post(
                CDSE_TOKEN_URL, data=data, timeout=30,
                headers={"User-Agent": USER_AGENT},
            )
        except requests.RequestException as exc:
            raise AuthError("Could not reach Copernicus identity service: "
                            "{}".format(exc))
        if resp.status_code != 200:
            try:
                detail = resp.json().get("error_description", resp.text[:200])
            except ValueError:
                detail = resp.text[:200]
            raise AuthError("Copernicus login failed: {}".format(detail))
        payload = resp.json()
        self._access_token = payload.get("access_token")
        self._refresh_token = payload.get("refresh_token",
                                          self._refresh_token)
        expires_in = int(payload.get("expires_in", 600))
        self._expiry = (
            datetime.datetime.utcnow()
            + datetime.timedelta(seconds=max(expires_in - 60, 60))
        )

    def logout(self):
        self._access_token = None
        self._refresh_token = None
        self._expiry = None


class UsgsAuth:
    """API-key manager for the USGS M2M API (login-token flow)."""

    def __init__(self):
        self.username = ""
        self.app_token = ""
        self._api_key = None
        self._expiry = None

    def configured(self):
        return bool(self.username and self.app_token)

    def connected(self):
        return (
            self._api_key is not None
            and self._expiry is not None
            and datetime.datetime.utcnow() < self._expiry
        )

    def login(self):
        if not self.configured():
            raise AuthError("USGS credentials are not set.")
        try:
            resp = requests.post(
                USGS_LOGIN_TOKEN_URL,
                json={"username": self.username, "token": self.app_token},
                timeout=30, headers={"User-Agent": USER_AGENT},
            )
        except requests.RequestException as exc:
            raise AuthError("Could not reach USGS M2M API: {}".format(exc))
        try:
            payload = resp.json()
        except ValueError:
            raise AuthError("USGS M2M API returned an unexpected response.")
        if payload.get("errorCode"):
            raise AuthError("USGS login failed: {} - {}".format(
                payload.get("errorCode"), payload.get("errorMessage", "")
            ))
        self._api_key = payload.get("data")
        #API keys are valid for 2 h; refresh a little early
        self._expiry = (datetime.datetime.utcnow()
                        + datetime.timedelta(minutes=110))

    def get_key(self):
        if not self.connected():
            self.login()
        return self._api_key

    def logout(self):
        self._api_key = None
        self._expiry = None
