"""Platform definitions for SatStream.

Each entry maps a dropdown label to a STAC endpoint, collection, and the
asset keys used for the Visual / Multispectral / Panchromatic / Download
actions.

Endpoints used:
  * Element84 Earth Search -- free, anonymous Sentinel-2 L2A COGs on AWS.
  * Microsoft Planetary Computer -- free; asset URLs are signed with an
    anonymous short-lived SAS token (stac_client.sign_href).
  * Copernicus Data Space Ecosystem (CDSE) -- free account required for
    downloads. Search is anonymous; product downloads use an OAuth
    Bearer token (credentials panel in the dock).

Per-provider keys:
  auth            None | "copernicus" | "usgs" -- which credentials page
                  to show below Platform Selection.
  auth_download   True if Download Selected requires those credentials.
  raw_download    True if the download is a whole product archive (zip)
                  fetched over authenticated HTTP instead of a
                  clipped GeoTIFF via GDAL.
  search_flavor   "stac" (full STAC query ext) or "cdse" (conservative
                  request; cloud/id filtering done client-side).
"""

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1"
PLANETARY = "https://planetarycomputer.microsoft.com/api/stac/v1"
CDSE_STAC = "https://catalogue.dataspace.copernicus.eu/stac"

#Sentinel-2 band asset keys on Earth Search v1 (COG collection)
_S2_MS_BANDS = [
    "coastal", "blue", "green", "red",
    "rededge1", "rededge2", "rededge3",
    "nir", "nir08", "nir09",
    "swir16", "swir22",
]

#Landsat C2 L2 band asset keys on Planetary Computer
_LS_MS_BANDS = ["coastal", "blue", "green", "red", "nir08", "swir16", "swir22"]

PROVIDERS = {
    "Sentinel-2 L1C": {
        "endpoint": CDSE_STAC,
        "collection": "SENTINEL-2",
        "search_flavor": "cdse",
        "id_contains": "L1C",
        "sign": False,
        "auth": "copernicus",
        "auth_download": True,
        "raw_download": True,
        "platform_in": None,
        "supports_cloud": True,
        "gsd_label": "10-60 m",
        "visual_assets": None,
        "rgb_assets": None,
        "ms_assets": None,
        "pan_assets": None,
        "download_assets": ["PRODUCT"],
        "note": (
            "Searched via the Copernicus Data Space catalog. Streaming is "
            "not available for L1C (no public COGs); Download Selected "
            "fetches the full product (.zip) using your Copernicus account."
        ),
    },
    "Sentinel-2 L2A": {
        "endpoint": EARTH_SEARCH,
        "collection": "sentinel-2-l2a",
        "search_flavor": "stac",
        "sign": False,
        "auth": None,
        "auth_download": False,
        "raw_download": False,
        "platform_in": None,
        "supports_cloud": True,
        "gsd_label": "10-60 m",
        "visual_assets": ["visual"],
        "rgb_assets": ["red", "green", "blue"],
        "ms_assets": _S2_MS_BANDS,
        "pan_assets": None,
        "download_assets": ["visual"],
        "note": "Free anonymous streaming (COGs on AWS). No login needed.",
    },
    "Sentinel-3": {
        "endpoint": CDSE_STAC,
        "collection": "SENTINEL-3",
        "search_flavor": "cdse",
        "sign": False,
        "auth": "copernicus",
        "auth_download": True,
        "raw_download": True,
        "platform_in": None,
        "supports_cloud": False,
        "gsd_label": "300 m",
        "visual_assets": None,
        "rgb_assets": None,
        "ms_assets": None,
        "pan_assets": None,
        "download_assets": ["PRODUCT"],
        "note": (
            "Sentinel-3 products are NetCDF archives: streaming is not "
            "supported. Download Selected fetches the product (.zip) with "
            "your Copernicus account."
        ),
    },
    "Sentinel-5p": {
        "endpoint": CDSE_STAC,
        "collection": "SENTINEL-5P",
        "search_flavor": "cdse",
        "sign": False,
        "auth": "copernicus",
        "auth_download": True,
        "raw_download": True,
        "platform_in": None,
        "supports_cloud": False,
        "gsd_label": "~5.5 x 3.5 km",
        "visual_assets": None,
        "rgb_assets": None,
        "ms_assets": None,
        "pan_assets": None,
        "download_assets": ["PRODUCT"],
        "note": (
            "Sentinel-5P products are NetCDF: streaming is not supported. "
            "Download Selected fetches the product with your Copernicus "
            "account."
        ),
    },
    "Copernicus DEM": {
        "endpoint": PLANETARY,
        "collection": "cop-dem-glo-30",
        "search_flavor": "stac",
        "sign": True,
        "auth": None,
        "auth_download": False,
        "raw_download": False,
        "platform_in": None,
        "supports_cloud": False,
        "gsd_label": "30 m",
        "visual_assets": ["data"],
        "rgb_assets": None,
        "ms_assets": None,
        "pan_assets": None,
        "download_assets": ["data"],
        "note": (
            "Elevation tiles (GLO-30) streamed anonymously from the "
            "Planetary Computer. 'Load Visual' streams the DEM band."
        ),
    },
    "Landsat 8": {
        "endpoint": PLANETARY,
        "collection": "landsat-c2-l2",
        "search_flavor": "stac",
        "sign": True,
        "auth": "usgs",
        "auth_download": False,
        "raw_download": False,
        "platform_in": ["landsat-8"],
        "supports_cloud": True,
        "gsd_label": "30 m",
        "visual_assets": None,          #no TCI asset, RGB VRT is built
        "rgb_assets": ["red", "green", "blue"],
        "ms_assets": _LS_MS_BANDS,
        "pan_assets": ["pan", "B8"],
        "download_assets": ["red"],
        "note": (
            "Level-2 streaming/download is free (no login). USGS M2M "
            "credentials are optional: they are validated and stored for "
            "Level-1 / 15 m panchromatic ordering (coming soon)."
        ),
    },
    "Landsat 9": {
        "endpoint": PLANETARY,
        "collection": "landsat-c2-l2",
        "search_flavor": "stac",
        "sign": True,
        "auth": "usgs",
        "auth_download": False,
        "raw_download": False,
        "platform_in": ["landsat-9"],
        "supports_cloud": True,
        "gsd_label": "30 m",
        "visual_assets": None,
        "rgb_assets": ["red", "green", "blue"],
        "ms_assets": _LS_MS_BANDS,
        "pan_assets": ["pan", "B8"],
        "download_assets": ["red"],
        "note": (
            "Level-2 streaming/download is free (no login). USGS M2M "
            "credentials are optional: they are validated and stored for "
            "Level-1 / 15 m panchromatic ordering (coming soon)."
        ),
    },
}

PROVIDER_ORDER = [
    "Sentinel-2 L1C",
    "Sentinel-2 L2A",
    "Sentinel-3",
    "Sentinel-5p",
    "Copernicus DEM",
    "Landsat 8",
    "Landsat 9",
]
