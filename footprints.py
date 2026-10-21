"""Scene footprints drawn on the map canvas, plus a map tool to pick them.

FootprintOverlay draws three layers of rubber bands:
  * every scene returned by the search (thin blue outline)
  * scenes selected in the results table (orange)
  * the scene under the cursor (yellow)

FootprintPickTool behaves like the normal pan tool (drag to pan, wheel to
zoom) but also reports hovers and single clicks so the dock can highlight
and select the footprint under the cursor.
"""

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)
from qgis.gui import QgsMapToolPan, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QCursor

try:  #QGIS 3.30 and newer
    from qgis.core import Qgis
    POLYGON = Qgis.GeometryType.Polygon
except AttributeError:  #older QGIS 3.x
    from qgis.core import QgsWkbTypes
    POLYGON = QgsWkbTypes.PolygonGeometry

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

#pixels the mouse may move between press and release and still count as a click
CLICK_TOLERANCE = 4


def geojson_to_geometry(geom_json):
    """Convert a GeoJSON Polygon or MultiPolygon dict to a QgsGeometry."""
    if not geom_json:
        return QgsGeometry()
    gtype = geom_json.get("type")
    coords = geom_json.get("coordinates", [])

    def ring(points):
        return [QgsPointXY(p[0], p[1]) for p in points]

    if gtype == "Polygon":
        return QgsGeometry.fromPolygonXY([ring(r) for r in coords])
    if gtype == "MultiPolygon":
        return QgsGeometry.fromMultiPolygonXY(
            [[ring(r) for r in poly] for poly in coords]
        )
    return QgsGeometry()


class FootprintOverlay:
    """Keeps the footprint rubber bands in sync with the search results."""

    def __init__(self, canvas):
        self.canvas = canvas
        self._scenes = []          #(scene, geometry in EPSG:4326)
        self._selected_ids = set()
        self._hover_id = None
        self._bands = {}
        self.canvas.destinationCrsChanged.connect(self.redraw)

    #public API

    def set_scenes(self, scenes):
        self._scenes = []
        for scene in scenes:
            geom = geojson_to_geometry(scene.get("geometry"))
            if not geom.isEmpty():
                self._scenes.append((scene, geom))
        self._selected_ids = set()
        self._hover_id = None
        self.redraw()

    def set_selected(self, scene_ids):
        self._selected_ids = set(scene_ids)
        self._draw("selected")

    def set_hover(self, scene_id):
        if scene_id != self._hover_id:
            self._hover_id = scene_id
            self._draw("hover")

    def scenes_at(self, map_point):
        """Scenes whose footprint contains a point in canvas coordinates,
        newest first."""
        to_wgs = QgsCoordinateTransform(
            self.canvas.mapSettings().destinationCrs(), WGS84,
            QgsProject.instance(),
        )
        try:
            point = QgsGeometry.fromPointXY(to_wgs.transform(map_point))
        except Exception:  #noqa: BLE001, point outside valid CRS bounds
            return []
        hits = [s for s, g in self._scenes if g.contains(point)]
        hits.sort(key=lambda s: s.get("sort_key") or s.get("datetime", ""),
                  reverse=True)
        return hits

    def has_scenes(self):
        return bool(self._scenes)

    def redraw(self):
        for name in ("all", "selected", "hover"):
            self._draw(name)

    def clear(self):
        for band in self._bands.values():
            band.reset(POLYGON)
            self.canvas.scene().removeItem(band)
        self._bands = {}

    def remove(self):
        self.clear()
        try:
            self.canvas.destinationCrsChanged.disconnect(self.redraw)
        except TypeError:
            pass

    #drawing

    _STYLES = {
        #name: (outline, fill, width, z order)
        "all": (QColor(30, 144, 255, 200), QColor(30, 144, 255, 18), 1, 10),
        "selected": (QColor(255, 140, 0, 230), QColor(255, 140, 0, 55), 2, 11),
        "hover": (QColor(255, 230, 0, 255), QColor(255, 230, 0, 70), 3, 12),
    }

    def _band(self, name):
        band = self._bands.get(name)
        if band is None:
            outline, fill, width, z_order = self._STYLES[name]
            band = QgsRubberBand(self.canvas, POLYGON)
            band.setStrokeColor(outline)
            band.setFillColor(fill)
            band.setWidth(width)
            band.setZValue(z_order)
            self._bands[name] = band
        return band

    def _draw(self, name):
        band = self._band(name)
        band.reset(POLYGON)
        if name == "all":
            wanted = self._scenes
        elif name == "selected":
            wanted = [(s, g) for s, g in self._scenes
                      if s["id"] in self._selected_ids]
        else:
            wanted = [(s, g) for s, g in self._scenes
                      if s["id"] == self._hover_id]
        if not wanted:
            return
        to_canvas = QgsCoordinateTransform(
            WGS84, self.canvas.mapSettings().destinationCrs(),
            QgsProject.instance(),
        )
        for _scene, geom in wanted:
            shown = QgsGeometry(geom)
            shown.transform(to_canvas)
            band.addGeometry(shown, None, False)
        band.updatePosition()
        band.update()


class FootprintPickTool(QgsMapToolPan):
    """Pan tool that also reports hovering and clicking on the map.

    hovered(map_point, global_pos) fires as the mouse moves with no button
    held. clicked(map_point, global_pos, modifiers) fires on a left click
    that did not turn into a drag.
    """

    hovered = pyqtSignal(object, object)
    clicked = pyqtSignal(object, object, object)

    def __init__(self, canvas):
        super().__init__(canvas)
        self._press_pos = None

    def canvasPressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.pos()
        super().canvasPressEvent(event)

    def canvasMoveEvent(self, event):
        super().canvasMoveEvent(event)
        if event.buttons() == Qt.NoButton:
            self.hovered.emit(event.mapPoint(), event.globalPos())

    def canvasReleaseEvent(self, event):
        press, self._press_pos = self._press_pos, None
        if (event.button() == Qt.LeftButton and press is not None
                and (event.pos() - press).manhattanLength()
                <= CLICK_TOLERANCE):
            #a click, not a drag: pick instead of panning
            self.clicked.emit(event.mapPoint(), event.globalPos(),
                              event.modifiers())
            self.canvas().setCursor(QCursor(Qt.OpenHandCursor))
            return
        super().canvasReleaseEvent(event)
