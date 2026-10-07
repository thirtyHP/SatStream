"""Background tasks (QgsTask) so searches and downloads never block the UI."""

import os

import requests
from osgeo import gdal
from qgis.core import QgsTask

from . import stac_client


class SearchTask(QgsTask):
    """Runs a STAC search in the background."""

    def __init__(self, cfg, bbox4326, start_date, end_date, max_cloud,
                 on_done):
        super().__init__("SatStream: searching imagery", QgsTask.CanCancel)
        self.cfg = cfg
        self.bbox = bbox4326
        self.start_date = start_date
        self.end_date = end_date
        self.max_cloud = max_cloud
        self.on_done = on_done
        self.results = []
        self.error = None

    def run(self):
        try:
            self.results = stac_client.search(
                self.cfg, self.bbox, self.start_date, self.end_date,
                self.max_cloud,
            )
            return True
        except Exception as exc:  #noqa: BLE001, report any failure to UI
            self.error = str(exc)
            return False

    def finished(self, ok):
        self.on_done(ok, self.results, self.error)


class DownloadTask(QgsTask):
    """Downloads one asset.

    Two modes:
      * raw=False (default): GDAL clip/translate of a COG via /vsicurl/,
        optionally clipped to the canvas extent. If `headers` is given
        (e.g. a Bearer token) it is applied via GDAL_HTTP_HEADERS.
      * raw=True: plain authenticated HTTP streaming download of a whole
        product archive (e.g. CDSE .zip via the OData endpoint). No
        clipping is possible in this mode.
    """

    def __init__(self, href, out_path, clip_bounds4326, on_done,
                 headers=None, raw=False):
        super().__init__(
            "SatStream: downloading {}".format(os.path.basename(out_path)),
            QgsTask.CanCancel,
        )
        self.href = href
        self.out_path = out_path
        self.clip_bounds = clip_bounds4326  #(xmin, ymin, xmax, ymax) or None
        self.on_done = on_done
        self.headers = headers or {}
        self.raw = raw
        self.error = None

    def run(self):
        try:
            if self.raw:
                return self._run_http()
            return self._run_gdal()
        except Exception as exc:  #noqa: BLE001
            self.error = str(exc)
            return False

    def _run_http(self):
        with requests.get(
            self.href, headers=self.headers, stream=True, timeout=120,
            allow_redirects=True,
        ) as resp:
            if resp.status_code in (401, 403):
                self.error = ("Server refused the download (HTTP {}). "
                              "Check your credentials / account access."
                              .format(resp.status_code))
                return False
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0) or 0)
            done = 0
            with open(self.out_path, "wb") as out:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if self.isCanceled():
                        return False
                    if chunk:
                        out.write(chunk)
                        done += len(chunk)
                        if total:
                            self.setProgress(done / total * 100.0)
        return os.path.exists(self.out_path)

    def _run_gdal(self):
        gdal.UseExceptions()
        src = "/vsicurl/" + self.href

        def _progress(complete, message, data):
            if self.isCanceled():
                return 0
            self.setProgress(complete * 100)
            return 1

        header_value = None
        if self.headers:
            header_value = "\r\n".join(
                "{}: {}".format(k, v) for k, v in self.headers.items()
            )
        try:
            if header_value:
                gdal.SetConfigOption("GDAL_HTTP_HEADERS", header_value)
            if self.clip_bounds:
                opts = gdal.WarpOptions(
                    outputBounds=self.clip_bounds,
                    outputBoundsSRS="EPSG:4326",
                    format="GTiff",
                    creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
                    callback=_progress,
                )
                gdal.Warp(self.out_path, src, options=opts)
            else:
                opts = gdal.TranslateOptions(
                    format="GTiff",
                    creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
                    callback=_progress,
                )
                gdal.Translate(self.out_path, src, options=opts)
        finally:
            if header_value:
                gdal.SetConfigOption("GDAL_HTTP_HEADERS", None)
        return os.path.exists(self.out_path)

    def finished(self, ok):
        self.on_done(ok, self.out_path, self.error)
