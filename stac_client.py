"""Minimal STAC client for SatStream.

Uses plain `requests` (bundled with QGIS) instead of pystac-client so the
plugin has no external dependencies. Handles:

  * POST /search against any STAC 1.x API
  * cloud-cover and platform filtering via the STAC "query" extension
  * signing of Microsoft Planetary Computer asset hrefs (SAS tokens)
"""

import datetime

import requests

SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"
_sign_cache = {}

USER_AGENT = "SatStream-QGIS-Plugin/0.1"


class StacError(RuntimeError):
    pass


def search(cfg, bbox4326, start_date, end_date, max_cloud, limit=100):
    """Search a STAC API and return a list of simplified scene dicts.

    :param cfg: provider config dict from providers.PROVIDERS
    :param bbox4326: (xmin, ymin, xmax, ymax) in EPSG:4326
    :param start_date, end_date: datetime.date
    :param max_cloud: int 0-100 (ignored if provider doesn't support it)
    :param limit: max number of items
    """
    flavor = cfg.get("search_flavor", "stac")
    body = {
        "collections": [cfg["collection"]],
        "bbox": list(bbox4326),
        "datetime": "{}T00:00:00Z/{}T23:59:59Z".format(
            start_date.isoformat(), end_date.isoformat()
        ),
        "limit": min(int(limit), 200),
    }

    if flavor == "stac":
        #Full STAC APIs (Earth Search, Planetary Computer): sort and
        #filter server-side via the query extension.
        body["sortby"] = [
            {"field": "properties.datetime", "direction": "desc"}
        ]
        query = {}
        if (cfg.get("supports_cloud") and max_cloud is not None
                and max_cloud < 100):
            query["eo:cloud_cover"] = {"lte": float(max_cloud)}
        if cfg.get("platform_in"):
            query["platform"] = {"in": cfg["platform_in"]}
        if query:
            body["query"] = query

    url = cfg["endpoint"].rstrip("/") + "/search"
    try:
        resp = requests.post(
            url, json=body, timeout=60, headers={"User-Agent": USER_AGENT}
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise StacError("STAC search failed: {}".format(exc))

    features = resp.json().get("features", [])
    scenes = []
    for feat in features:
        props = feat.get("properties", {})
        dt = props.get("datetime") or props.get("start_datetime") or ""
        cloud = props.get("eo:cloud_cover")
        if cloud is None:
            cloud = props.get("cloudCover")  #CDSE property name
        scenes.append(
            {
                "id": feat.get("id", ""),
                "collection": feat.get("collection", cfg["collection"]),
                "datetime": _short_date(dt),
                "sort_key": dt,
                "platform": props.get("platform", ""),
                "gsd": props.get("gsd"),
                "cloud": cloud,
                "geometry": feat.get("geometry"),
                "bbox": feat.get("bbox"),
                "assets": feat.get("assets", {}),
            }
        )

    if flavor == "cdse":
        #CDSE's STAC endpoint doesn't reliably support the query/sortby
        #extensions, so filter and sort client-side.
        id_contains = cfg.get("id_contains")
        if id_contains:
            scenes = [s for s in scenes if id_contains in s["id"]]
        if (cfg.get("supports_cloud") and max_cloud is not None
                and max_cloud < 100):
            scenes = [
                s for s in scenes
                if s["cloud"] is None or float(s["cloud"]) <= float(max_cloud)
            ]
        scenes.sort(key=lambda s: s.get("sort_key", ""), reverse=True)
    return scenes


def _short_date(iso_string):
    try:
        return iso_string[:10]
    except Exception:
        return str(iso_string)


def sign_href(href, needs_signing):
    """Return a streamable href, signing Planetary Computer URLs on demand.

    SAS tokens are short-lived, so a tiny cache with expiry is kept.
    """
    if not needs_signing:
        return href

    cached = _sign_cache.get(href)
    if cached:
        signed, expires = cached
        if datetime.datetime.utcnow() < expires:
            return signed

    try:
        resp = requests.get(
            SIGN_URL, params={"href": href}, timeout=30,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
        signed = data.get("href", href)
        #tokens usually last ~1 h; refresh after 45 min to be safe
        expires = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)
        _sign_cache[href] = (signed, expires)
        return signed
    except requests.RequestException as exc:
        raise StacError("Could not sign asset URL: {}".format(exc))


def pick_asset(assets, keys):
    """Return (key, href) for the first matching asset key, else (None, None)."""
    if not keys:
        return None, None
    for key in keys:
        if key in assets and assets[key].get("href"):
            return key, assets[key]["href"]
    return None, None
