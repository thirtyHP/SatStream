# SatStream — QGIS Plugin

Stream, clip, and process open-source Sentinel and Landsat imagery for the
current map extent, straight from public STAC catalogs.

USE

1. **Platform Selection** — dropdown for Sentinel-2 L1C, Sentinel-2 L2A,
   Sentinel-3, Sentinel-5p, Copernicus DEM, Landsat 8, and Landsat 9.
2. **Filters** — max cloud cover slider (0–100 %) plus start/end date.
3. **Imagery Footprint** — results table with Date, Platform, GSD,
   Cloud %, and Catalog ID. Selecting rows highlights the scene
   footprints on the canvas.
4. **Tools** — Zoom to Selected Layer, Load Visual, Load Multispectral,
   Load Panchromatic, Download Selected (optionally clipped to the map
   extent), and Clear Layers.

## Data sources

| Platform          | Catalog                      | Collection                     |
|-------------------|------------------------------|--------------------------------|
| Sentinel-2 L1C    | Element84 Earth Search       | `sentinel-2-l1c`               |
| Sentinel-2 L2A    | Element84 Earth Search       | `sentinel-2-l2a`               |
| Sentinel-3        | Microsoft Planetary Computer | `sentinel-3-olci-lfr-l2-netcdf`|
| Sentinel-5p       | Microsoft Planetary Computer | `sentinel-5p-l2-netcdf`        |
| Copernicus DEM    | Microsoft Planetary Computer | `cop-dem-glo-30`               |
| Landsat 8 / 9     | Microsoft Planetary Computer | `landsat-c2-l2`                |

No API keys are required. Planetary Computer asset URLs are signed
automatically with anonymous short-lived SAS tokens.

## Install

1. Zip the `satstream` folder (or use the provided `satstream.zip`).
2. In QGIS: *Plugins → Manage and Install Plugins → Install from ZIP*.
3. Enable **SatStream** and click the toolbar button to open the dock.

Dependencies: only what ships with QGIS (`requests`, GDAL Python bindings,
PyQt). No `pystac-client` needed — STAC search is done with plain HTTP.

## Credentials

A **Credentials** panel sits directly below *Platform Selection* and swaps
automatically based on the selected platform:

- **Sentinel-2 L1C / Sentinel-3 / Sentinel-5p** → Copernicus Data Space
  Ecosystem (CDSE) email + password. Free account at
  https://dataspace.copernicus.eu. SatStream exchanges these for a
  short-lived OAuth token (auto-refreshed) and uses it as a Bearer token
  for product downloads via the CDSE OData endpoint.
- **Landsat 8 / 9** → USGS EROS username + M2M *application token*
  (generated in your EROS profile — not your password). Free account at
  https://ers.cr.usgs.gov with M2M access requested. Level-2
  streaming/downloads stay anonymous via the Planetary Computer; the
  USGS key is validated/stored for upcoming Level-1 (15 m pan) ordering.
- **Sentinel-2 L2A / Copernicus DEM** → no credentials required.

"Remember on this machine" saves the values in QGIS settings
(**not encrypted** — leave unchecked on shared machines; a future version
can move this into the QGIS Authentication database).

## How streaming works

- Scenes are Cloud-Optimized GeoTIFFs loaded through GDAL `/vsicurl/`, so
  QGIS only fetches the tiles/overviews needed for the current view.
- **Load Visual** uses the true-color (TCI) asset when available; for
  Landsat (no TCI asset on Planetary Computer) an RGB VRT is built from
  the red/green/blue band COGs.
- **Load Multispectral** builds a temporary multi-band VRT
  (`gdal.BuildVRT(separate=True, resolution="highest")`) stacking all
  band assets, so mixed 10/20/60 m Sentinel-2 bands land in one layer.
- **Download Selected** runs `gdal.Warp` in a background `QgsTask`,
  optionally clipped to the canvas extent (`outputBounds` in EPSG:4326),
  written as tiled + DEFLATE-compressed GeoTIFF.


