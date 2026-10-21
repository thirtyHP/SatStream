"""SatStream dock widget: platform selection, filters, footprint table, tools."""

import os
import re
import tempfile

from osgeo import gdal
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsSettings,
)
from qgis.PyQt.QtCore import (
    QDate,
    QItemSelection,
    QItemSelectionModel,
    Qt,
)
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDockWidget,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import stac_client
from .auth import AuthError, CopernicusAuth, UsgsAuth
from .footprints import FootprintOverlay, FootprintPickTool
from .providers import PROVIDER_ORDER, PROVIDERS
from .tasks import DownloadTask, SearchTask

GROUP_NAME = "SatStream"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


class SatStreamDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("SatStream", parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self._tasks = []          #keep task refs so they aren't GC'd
        self._layer_ids = []      #layers added by SatStream
        self._scenes = []         #results of the last search
        self._cycle_ids = ()      #overlapping scenes under the cursor
        self._cycle_index = 0
        self._cycle_clicked = False
        self._prev_tool = None
        self.footprints = FootprintOverlay(self.canvas)
        self.pick_tool = FootprintPickTool(self.canvas)
        self.pick_tool.hovered.connect(self._on_map_hover)
        self.pick_tool.clicked.connect(self._on_map_click)
        self.canvas.mapToolSet.connect(self._on_map_tool_changed)
        self._temp_dir = tempfile.mkdtemp(prefix="satstream_")
        self.cop_auth = CopernicusAuth()
        self.usgs_auth = UsgsAuth()
        self._build_ui()
        self._load_saved_credentials()

    #UI

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)

        #Platform Selection
        plat_box = QGroupBox("Platform Selection")
        plat_layout = QVBoxLayout(plat_box)
        self.platform_combo = QComboBox()
        self.platform_combo.addItems(PROVIDER_ORDER)
        self.platform_combo.setCurrentText("Sentinel-2 L2A")
        self.platform_combo.currentTextChanged.connect(self._on_platform_changed)
        plat_layout.addWidget(self.platform_combo)
        self.platform_note = QLabel("")
        self.platform_note.setWordWrap(True)
        self.platform_note.setSizePolicy(QSizePolicy.Preferred,
                                         QSizePolicy.MinimumExpanding)
        self.platform_note.setStyleSheet("color: gray; font-size: 8pt;")
        plat_layout.addWidget(self.platform_note)
        layout.addWidget(plat_box)

        #Credentials (shown per platform family)
        layout.addWidget(self._build_credentials_box())

        #Filters
        filt_box = QGroupBox("Filters")
        filt_layout = QGridLayout(filt_box)

        filt_layout.addWidget(QLabel("Max Cloud Cover:"), 0, 0)
        self.cloud_slider = QSlider(Qt.Horizontal)
        self.cloud_slider.setRange(0, 100)
        self.cloud_slider.setValue(20)
        self.cloud_label = QLabel("20 %")
        self.cloud_slider.valueChanged.connect(
            lambda v: self.cloud_label.setText("{} %".format(v))
        )
        filt_layout.addWidget(self.cloud_slider, 0, 1)
        filt_layout.addWidget(self.cloud_label, 0, 2)

        filt_layout.addWidget(QLabel("Start Date:"), 1, 0)
        self.start_date = QDateEdit(QDate.currentDate().addMonths(-1))
        self.start_date.setCalendarPopup(True)
        filt_layout.addWidget(self.start_date, 1, 1, 1, 2)

        filt_layout.addWidget(QLabel("End Date:"), 2, 0)
        self.end_date = QDateEdit(QDate.currentDate())
        self.end_date.setCalendarPopup(True)
        filt_layout.addWidget(self.end_date, 2, 1, 1, 2)

        self.search_btn = QPushButton("Search Map Extent")
        self.search_btn.clicked.connect(self._on_search)
        filt_layout.addWidget(self.search_btn, 3, 0, 1, 3)
        layout.addWidget(filt_box)

        #Imagery Footprint
        fp_box = QGroupBox("Imagery Footprint")
        fp_layout = QVBoxLayout(fp_box)
        self.table = QTableWidget(0, 5)
        self.table.setMinimumHeight(120)
        self.table.setHorizontalHeaderLabels(
            ["Date", "Platform", "GSD", "Cloud %", "Catalog ID"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        fp_layout.addWidget(self.table)
        self.pick_check = QCheckBox("Pick footprints on the map")
        self.pick_check.setChecked(True)
        self.pick_check.setToolTip(
            "Hover over a footprint to highlight it and click to select just "
            "that scene. Ctrl+click (Cmd+click on Mac) adds or removes a "
            "scene. Click again on overlapping footprints to step through "
            "them. Drag to pan as usual."
        )
        self.pick_check.toggled.connect(self._on_pick_toggled)
        fp_layout.addWidget(self.pick_check)
        select_row = QHBoxLayout()
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.setToolTip(
            "Select every scene in the results table."
        )
        self.select_all_btn.clicked.connect(self._on_select_all)
        #one button per sensor in the results, rebuilt after each search
        self.sensor_btn_layout = QHBoxLayout()
        self.sensor_btn_layout.setContentsMargins(0, 0, 0, 0)
        self.sensor_btn_layout.setSpacing(4)
        self._sensor_btns = []
        select_row.addLayout(self.sensor_btn_layout)
        select_row.addStretch()
        select_row.addWidget(self.select_all_btn)
        fp_layout.addLayout(select_row)
        self.status_label = QLabel("Ready.")
        self.status_label.setWordWrap(True)
        fp_layout.addWidget(self.status_label)
        layout.addWidget(fp_box, stretch=1)

        #Tools
        tools_box = QGroupBox("Tools")
        tools_layout = QVBoxLayout(tools_box)

        zoom_row = QHBoxLayout()
        self.zoom_btn = QPushButton("Zoom to Selected Layer")
        self.zoom_btn.clicked.connect(self._on_zoom)
        zoom_row.addStretch()
        zoom_row.addWidget(self.zoom_btn)
        zoom_row.addStretch()
        tools_layout.addLayout(zoom_row)

        load_row = QHBoxLayout()
        self.visual_btn = QPushButton("Load Visual")
        self.visual_btn.clicked.connect(self._on_load_visual)
        self.ms_btn = QPushButton("Load Multispectral")
        self.ms_btn.clicked.connect(self._on_load_multispectral)
        self.pan_btn = QPushButton("Load Panchromatic")
        self.pan_btn.clicked.connect(self._on_load_panchromatic)
        load_row.addWidget(self.visual_btn)
        load_row.addWidget(self.ms_btn)
        load_row.addWidget(self.pan_btn)
        tools_layout.addLayout(load_row)

        dl_row = QHBoxLayout()
        self.clip_check = QCheckBox("Clip to map extent")
        self.clip_check.setChecked(True)
        self.download_btn = QPushButton("Download Selected")
        self.download_btn.clicked.connect(self._on_download)
        dl_row.addStretch()
        dl_row.addWidget(self.clip_check)
        dl_row.addWidget(self.download_btn)
        dl_row.addStretch()
        tools_layout.addLayout(dl_row)

        clear_row = QHBoxLayout()
        self.clear_btn = QPushButton("Clear Layers")
        self.clear_btn.clicked.connect(self._on_clear_layers)
        clear_row.addStretch()
        clear_row.addWidget(self.clear_btn)
        clear_row.addStretch()
        tools_layout.addLayout(clear_row)

        layout.addWidget(tools_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(root)
        #never narrower than the widest row, so nothing is clipped
        scroll.setMinimumWidth(
            root.minimumSizeHint().width()
            + scroll.verticalScrollBar().sizeHint().width()
        )
        self.setWidget(scroll)
        self._on_platform_changed(self.platform_combo.currentText())

    #credentials

    def _build_credentials_box(self):
        self.cred_box = QGroupBox("Credentials")
        box_layout = QVBoxLayout(self.cred_box)
        box_layout.setContentsMargins(6, 4, 6, 4)
        box_layout.setSpacing(2)
        self.cred_stack = QStackedWidget()

        #Page 0: no credentials required
        none_page = QWidget()
        none_layout = QVBoxLayout(none_page)
        none_layout.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel("No credentials required for this platform.")
        lbl.setStyleSheet("color: gray; font-size: 8pt;")
        none_layout.addWidget(lbl)
        self.cred_stack.addWidget(none_page)

        #Page 1: Copernicus Data Space Ecosystem
        (cop_page, self.cop_user_edit, self.cop_pass_edit,
         self.cop_remember, self.cop_connect_btn, self.cop_status) = (
            self._make_login_page(
                "Email:", "Password:",
                'Not connected. Free account at <a href="https://dataspace.'
                'copernicus.eu">dataspace.copernicus.eu</a>',
                self._connect_copernicus,
            )
        )
        self.cred_stack.addWidget(cop_page)

        #Page 2: USGS EROS / M2M
        (usgs_page, self.usgs_user_edit, self.usgs_token_edit,
         self.usgs_remember, self.usgs_connect_btn, self.usgs_status) = (
            self._make_login_page(
                "Username:", "App token:",
                'Not connected. Free account at <a href="https://ers.cr.'
                'usgs.gov">ers.cr.usgs.gov</a> (request M2M access)',
                self._connect_usgs,
            )
        )
        self.usgs_token_edit.setToolTip(
            "Your M2M application token generated in your EROS profile "
            "(not your login password)."
        )
        self.cred_stack.addWidget(usgs_page)

        box_layout.addWidget(self.cred_stack)
        return self.cred_box

    def _make_login_page(self, user_label, secret_label, status_html,
                         on_connect):
        """Build a compact three row login form."""
        page = QWidget()
        grid = QGridLayout(page)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)

        grid.addWidget(QLabel(user_label), 0, 0)
        user_edit = QLineEdit()
        grid.addWidget(user_edit, 0, 1)

        grid.addWidget(QLabel(secret_label), 1, 0)
        secret_edit = QLineEdit()
        secret_edit.setEchoMode(QLineEdit.Password)
        grid.addWidget(secret_edit, 1, 1)

        row = QHBoxLayout()
        remember = QCheckBox("Remember")
        remember.setToolTip(
            "Stores the credentials in your QGIS settings (not encrypted). "
            "Leave unchecked on shared machines."
        )
        connect_btn = QPushButton("Connect")
        connect_btn.clicked.connect(on_connect)
        row.addWidget(remember)
        row.addStretch()
        row.addWidget(connect_btn)
        grid.addLayout(row, 2, 0, 1, 2)

        status = QLabel(status_html)
        status.setOpenExternalLinks(True)
        status.setWordWrap(True)
        status.setStyleSheet("font-size: 8pt;")
        grid.addWidget(status, 3, 0, 1, 2)
        return page, user_edit, secret_edit, remember, connect_btn, status

    def _fit_platform_note(self):
        """Give the wrapped note enough height for all of its lines."""
        note = self.platform_note
        note.ensurePolished()
        width = note.width() if note.width() > 50 else 280
        note.setMinimumHeight(note.heightForWidth(width))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "platform_note"):
            self._fit_platform_note()

    def _fit_cred_stack(self):
        """Size the stack to the visible page instead of the tallest one."""
        current = self.cred_stack.currentWidget()
        for i in range(self.cred_stack.count()):
            page = self.cred_stack.widget(i)
            if page is current:
                page.setSizePolicy(QSizePolicy.Preferred,
                                   QSizePolicy.Maximum)
            else:
                page.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.cred_stack.setFixedHeight(current.sizeHint().height())
        self.cred_box.adjustSize()

    def _connect_copernicus(self):
        self.cop_auth.username = self.cop_user_edit.text().strip()
        self.cop_auth.password = self.cop_pass_edit.text()
        if not self.cop_auth.configured():
            self.cop_status.setText("Enter your email and password first.")
            return
        self.cop_connect_btn.setEnabled(False)
        self.cop_status.setText("Connecting...")
        try:
            self.cop_auth.login()
        except AuthError as exc:
            self.cop_status.setText(
                '<span style="color:#b00;">{}</span>'.format(exc)
            )
            return
        finally:
            self.cop_connect_btn.setEnabled(True)
        self.cop_status.setText(
            '<span style="color:#070;">Connected as {} '
            '(token auto-refreshes).</span>'.format(self.cop_auth.username)
        )
        self._save_credentials()

    def _connect_usgs(self):
        self.usgs_auth.username = self.usgs_user_edit.text().strip()
        self.usgs_auth.app_token = self.usgs_token_edit.text().strip()
        if not self.usgs_auth.configured():
            self.usgs_status.setText(
                "Enter your username and application token first."
            )
            return
        self.usgs_connect_btn.setEnabled(False)
        self.usgs_status.setText("Connecting...")
        try:
            self.usgs_auth.login()
        except AuthError as exc:
            self.usgs_status.setText(
                '<span style="color:#b00;">{}</span>'.format(exc)
            )
            return
        finally:
            self.usgs_connect_btn.setEnabled(True)
        self.usgs_status.setText(
            '<span style="color:#070;">Connected as {} '
            '(API key valid ~2 h, auto-renewed).</span>'.format(
                self.usgs_auth.username
            )
        )
        self._save_credentials()

    def _save_credentials(self):
        settings = QgsSettings()
        if self.cop_remember.isChecked():
            settings.setValue("SatStream/cop_user", self.cop_user_edit.text())
            settings.setValue("SatStream/cop_pass", self.cop_pass_edit.text())
        else:
            settings.remove("SatStream/cop_user")
            settings.remove("SatStream/cop_pass")
        if self.usgs_remember.isChecked():
            settings.setValue("SatStream/usgs_user",
                              self.usgs_user_edit.text())
            settings.setValue("SatStream/usgs_token",
                              self.usgs_token_edit.text())
        else:
            settings.remove("SatStream/usgs_user")
            settings.remove("SatStream/usgs_token")

    def _load_saved_credentials(self):
        settings = QgsSettings()
        cop_user = settings.value("SatStream/cop_user", "")
        cop_pass = settings.value("SatStream/cop_pass", "")
        if cop_user:
            self.cop_user_edit.setText(cop_user)
            self.cop_pass_edit.setText(cop_pass)
            self.cop_remember.setChecked(True)
            self.cop_auth.username = cop_user
            self.cop_auth.password = cop_pass
            self.cop_status.setText(
                "Saved credentials loaded - click Connect to sign in."
            )
        usgs_user = settings.value("SatStream/usgs_user", "")
        usgs_token = settings.value("SatStream/usgs_token", "")
        if usgs_user:
            self.usgs_user_edit.setText(usgs_user)
            self.usgs_token_edit.setText(usgs_token)
            self.usgs_remember.setChecked(True)
            self.usgs_auth.username = usgs_user
            self.usgs_auth.app_token = usgs_token
            self.usgs_status.setText(
                "Saved credentials loaded - click Connect to sign in."
            )

    #helpers

    def _cfg(self):
        return PROVIDERS[self.platform_combo.currentText()]

    def _on_platform_changed(self, name):
        cfg = PROVIDERS[name]
        self.platform_note.setText(cfg.get("note", ""))
        self._fit_platform_note()
        self.cloud_slider.setEnabled(cfg.get("supports_cloud", False))
        self.cloud_label.setEnabled(cfg.get("supports_cloud", False))
        page = {"copernicus": 1, "usgs": 2}.get(cfg.get("auth"), 0)
        self.cred_stack.setCurrentIndex(page)
        self._fit_cred_stack()
        self.cred_box.setTitle(
            "Credentials" if page == 0 else
            "Credentials ({})".format(
                "Copernicus Data Space" if page == 1 else "USGS EROS"
            )
        )

    def _canvas_bbox_4326(self):
        extent = self.canvas.extent()
        crs = self.canvas.mapSettings().destinationCrs()
        if crs != WGS84:
            xform = QgsCoordinateTransform(crs, WGS84, QgsProject.instance())
            extent = xform.transformBoundingBox(extent)
        return (extent.xMinimum(), extent.yMinimum(),
                extent.xMaximum(), extent.yMaximum())

    def _selected_scenes(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        scenes = []
        for row in rows:
            item = self.table.item(row, 0)
            if item is not None:
                scene = item.data(Qt.UserRole)
                if scene:
                    scenes.append(scene)
        return scenes

    def _require_selection(self):
        scenes = self._selected_scenes()
        if not scenes:
            QMessageBox.information(
                self, "SatStream", "Select one or more rows in the table first."
            )
        return scenes

    def _set_status(self, text):
        self.status_label.setText(text)

    def _group(self):
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(GROUP_NAME)
        if group is None:
            group = root.insertGroup(0, GROUP_NAME)
        return group

    def _add_layer(self, layer):
        if not layer.isValid():
            QMessageBox.warning(
                self, "SatStream",
                "Layer failed to load. The source may require credentials "
                "or may not be a streamable COG.\n\n" + layer.source()[:200],
            )
            return False
        QgsProject.instance().addMapLayer(layer, addToLegend=False)
        self._group().insertLayer(0, layer)
        self._layer_ids.append(layer.id())
        return True

    #search

    def _on_search(self):
        cfg = self._cfg()
        bbox = self._canvas_bbox_4326()
        start = self.start_date.date().toPyDate()
        end = self.end_date.date().toPyDate()
        if start > end:
            QMessageBox.warning(self, "SatStream",
                                "Start date must be before end date.")
            return
        self.search_btn.setEnabled(False)
        self._set_status("Searching {} ...".format(cfg["collection"]))

        task = SearchTask(cfg, bbox, start, end,
                          self.cloud_slider.value(), self._search_done)
        self._tasks.append(task)
        QgsApplication.taskManager().addTask(task)

    def _search_done(self, ok, scenes, error):
        self.search_btn.setEnabled(True)
        if not ok:
            self._set_status("Search failed: {}".format(error))
            return
        self._scenes = scenes
        self._reset_cycle()
        self._populate_table(scenes)
        self.footprints.set_scenes(scenes)
        if not scenes:
            self._set_status("No scenes found for the current extent.")
            return
        if self.pick_check.isChecked():
            self._activate_pick_tool()
        self._set_status(
            "{} scene(s) found. Hover over a footprint on the map and click "
            "to select it.".format(len(scenes))
        )

    def _populate_table(self, scenes):
        cfg = self._cfg()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for scene in scenes:
            row = self.table.rowCount()
            self.table.insertRow(row)

            date_item = QTableWidgetItem(scene["datetime"])
            date_item.setData(Qt.UserRole, scene)
            gsd = scene.get("gsd")
            gsd_text = "{:g} m".format(gsd) if gsd else cfg["gsd_label"]
            cloud = scene.get("cloud")
            cloud_text = "{:.1f}".format(cloud) if cloud is not None else "-"

            self.table.setItem(row, 0, date_item)
            self.table.setItem(
                row, 1, QTableWidgetItem(scene["platform"] or _sensor_key(scene))
            )
            self.table.setItem(row, 2, QTableWidgetItem(gsd_text))
            self.table.setItem(row, 3, QTableWidgetItem(cloud_text))
            self.table.setItem(row, 4, QTableWidgetItem(scene["id"]))
        self.table.setSortingEnabled(True)
        self._rebuild_sensor_buttons(scenes)

    def _rebuild_sensor_buttons(self, scenes):
        """Create a select button for every sensor found in the results."""
        for btn in self._sensor_btns:
            self.sensor_btn_layout.removeWidget(btn)
            btn.deleteLater()
        self._sensor_btns = []

        counts = {}
        for scene in scenes:
            key = _sensor_key(scene)
            if key:
                counts[key] = counts.get(key, 0) + 1

        for key in sorted(counts):
            btn = QPushButton(_sensor_label(key))
            btn.setToolTip("Select all {} scene(s) from {}".format(
                counts[key], key))
            btn.clicked.connect(
                lambda _checked=False, k=key: self._on_select_sensor(k)
            )
            self.sensor_btn_layout.addWidget(btn)
            self._sensor_btns.append(btn)

    def _on_select_sensor(self, key):
        model = self.table.model()
        selection = QItemSelection()
        last_col = self.table.columnCount() - 1
        count = 0
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            scene = item.data(Qt.UserRole) if item else None
            if scene and _sensor_key(scene) == key:
                selection.select(model.index(row, 0),
                                 model.index(row, last_col))
                count += 1
        self.table.selectionModel().select(
            selection,
            QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
        )
        self.table.setFocus()
        self._set_status("Selected {} scene(s) from {}.".format(
            count, _sensor_label(key)))

    def _on_select_all(self):
        if self.table.rowCount() == 0:
            self._set_status("No scenes to select - run a search first.")
            return
        self.table.selectAll()
        self.table.setFocus()
        self._set_status(
            "Selected all {} scene(s).".format(self.table.rowCount())
        )

    #footprints on the map

    def _on_row_selected(self):
        self.footprints.set_selected(s["id"] for s in self._selected_scenes())

    def _activate_pick_tool(self):
        if self.canvas.mapTool() is not self.pick_tool:
            self._prev_tool = self.canvas.mapTool()
            self.canvas.setMapTool(self.pick_tool)

    def _deactivate_pick_tool(self):
        if self.canvas.mapTool() is self.pick_tool:
            restored = False
            if self._prev_tool is not None:
                try:
                    self.canvas.setMapTool(self._prev_tool)
                    restored = True
                except RuntimeError:  #previous tool was deleted
                    pass
            if not restored:
                self.iface.actionPan().trigger()
        self._prev_tool = None
        self.footprints.set_hover(None)
        QToolTip.hideText()

    def _on_pick_toggled(self, checked):
        if checked and self.footprints.has_scenes():
            self._activate_pick_tool()
        elif not checked:
            self._deactivate_pick_tool()

    def _on_map_tool_changed(self, new_tool, old_tool=None):
        #the user switched from the picker to another QGIS tool, so
        #reflect that in the checkbox
        if (old_tool is self.pick_tool and new_tool is not self.pick_tool
                and self.pick_check.isChecked()):
            self.pick_check.blockSignals(True)
            self.pick_check.setChecked(False)
            self.pick_check.blockSignals(False)
            self._prev_tool = None
            self.footprints.set_hover(None)
            QToolTip.hideText()

    def _reset_cycle(self, ids=()):
        self._cycle_ids = ids
        self._cycle_index = 0
        self._cycle_clicked = False

    def _current_pick(self, candidates, advance):
        """Choose which overlapping scene the cursor refers to.

        The newest scene comes first. Clicking again on the same stack of
        overlapping footprints steps to the next one.
        """
        ids = tuple(s["id"] for s in candidates)
        if ids != self._cycle_ids:
            self._reset_cycle(ids)
        if advance:
            if self._cycle_clicked:
                self._cycle_index = (self._cycle_index + 1) % len(ids)
            self._cycle_clicked = True
        return candidates[self._cycle_index]

    def _pick_tooltip(self, scene, count):
        cloud = scene.get("cloud")
        lines = ["{}  |  {}{}".format(
            scene.get("datetime", ""),
            _sensor_label(_sensor_key(scene)) or scene.get("platform", ""),
            "  |  {:.1f}% cloud".format(cloud) if cloud is not None else "",
        ), scene.get("id", "")]
        if count > 1:
            lines.append("{} of {} overlapping, click again for the next"
                         .format(self._cycle_index + 1, count))
        return "\n".join(lines)

    def _on_map_hover(self, map_point, global_pos):
        candidates = self.footprints.scenes_at(map_point)
        if not candidates:
            self.footprints.set_hover(None)
            self._reset_cycle()
            QToolTip.hideText()
            return
        scene = self._current_pick(candidates, advance=False)
        self.footprints.set_hover(scene["id"])
        QToolTip.showText(global_pos,
                          self._pick_tooltip(scene, len(candidates)),
                          self.canvas)

    def _on_map_click(self, map_point, global_pos, modifiers):
        candidates = self.footprints.scenes_at(map_point)
        if not candidates:
            return
        scene = self._current_pick(candidates, advance=True)
        self.footprints.set_hover(scene["id"])
        additive = bool(modifiers & (Qt.ControlModifier | Qt.MetaModifier))
        self._select_scene_row(scene["id"], additive)
        QToolTip.showText(global_pos,
                          self._pick_tooltip(scene, len(candidates)),
                          self.canvas)

    def _select_scene_row(self, scene_id, additive):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            scene = item.data(Qt.UserRole) if item else None
            if scene and scene["id"] == scene_id:
                break
        else:
            return
        model = self.table.model()
        selection = QItemSelection(
            model.index(row, 0),
            model.index(row, self.table.columnCount() - 1),
        )
        if additive:
            flags = QItemSelectionModel.Toggle | QItemSelectionModel.Rows
        else:
            flags = (QItemSelectionModel.ClearAndSelect
                     | QItemSelectionModel.Rows)
        self.table.selectionModel().select(selection, flags)
        self.table.scrollToItem(item)
        count = len(self._selected_scenes())
        self._set_status("{} scene(s) selected. Latest pick: {}".format(
            count, scene_id))

    def suspend_map(self):
        """Hide footprints and hand the map back when the panel closes."""
        self._deactivate_pick_tool()
        self.footprints.clear()

    def resume_map(self):
        """Redraw footprints when the panel is shown again."""
        if self._scenes:
            self.footprints.redraw()
            self._on_row_selected()
            if self.pick_check.isChecked():
                self._activate_pick_tool()

    def cleanup(self):
        self.suspend_map()
        self.footprints.remove()
        try:
            self.canvas.mapToolSet.disconnect(self._on_map_tool_changed)
        except TypeError:
            pass

    #tools

    def _on_zoom(self):
        scenes = self._require_selection()
        if not scenes:
            return
        rect = None
        for scene in scenes:
            bbox = scene.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            r = QgsRectangle(bbox[0], bbox[1], bbox[2], bbox[3])
            if rect is None:
                rect = QgsRectangle(r)
            else:
                rect.combineExtentWith(r)
        if rect is None:
            return
        xform = QgsCoordinateTransform(
            WGS84, self.canvas.mapSettings().destinationCrs(),
            QgsProject.instance(),
        )
        self.canvas.setExtent(xform.transformBoundingBox(rect))
        self.canvas.refresh()

    def _on_load_visual(self):
        cfg = self._cfg()
        for scene in self._require_selection():
            key, href = stac_client.pick_asset(scene["assets"],
                                               cfg.get("visual_assets"))
            if href:
                self._load_stream(scene, href, cfg, "visual")
            elif cfg.get("rgb_assets"):
                self._load_vrt(scene, cfg["rgb_assets"], cfg, "visual-rgb")
            else:
                self._set_status(
                    "No streamable visual product for {}. {}".format(
                        scene["id"], cfg.get("note", "")
                    )
                )

    def _on_load_multispectral(self):
        cfg = self._cfg()
        if not cfg.get("ms_assets"):
            QMessageBox.information(
                self, "SatStream",
                "Multispectral streaming is not available for this platform. "
                + cfg.get("note", ""),
            )
            return
        for scene in self._require_selection():
            self._load_vrt(scene, cfg["ms_assets"], cfg, "multispectral")

    def _on_load_panchromatic(self):
        cfg = self._cfg()
        scenes = self._require_selection()
        if not scenes:
            return
        for scene in scenes:
            key, href = stac_client.pick_asset(scene["assets"],
                                               cfg.get("pan_assets"))
            if href:
                self._load_stream(scene, href, cfg, "pan")
            else:
                QMessageBox.information(
                    self, "SatStream",
                    "No panchromatic asset available for {}.\n{}".format(
                        scene["id"], cfg.get("note", "")
                    ),
                )
                return

    def _load_stream(self, scene, href, cfg, suffix):
        try:
            signed = stac_client.sign_href(href, cfg.get("sign", False))
        except stac_client.StacError as exc:
            self._set_status(str(exc))
            return
        uri = "/vsicurl/" + signed
        name = "{} [{}]".format(scene["id"], suffix)
        self._set_status("Streaming {} ...".format(name))
        layer = QgsRasterLayer(uri, name, "gdal")
        if self._add_layer(layer):
            self._set_status("Loaded {}.".format(name))

    def _load_vrt(self, scene, band_keys, cfg, suffix):
        """Stack individual band COGs into a temporary VRT and stream it."""
        sources = []
        for key in band_keys:
            if key in scene["assets"] and scene["assets"][key].get("href"):
                try:
                    signed = stac_client.sign_href(
                        scene["assets"][key]["href"], cfg.get("sign", False)
                    )
                except stac_client.StacError as exc:
                    self._set_status(str(exc))
                    return
                sources.append("/vsicurl/" + signed)
        if not sources:
            self._set_status("No band assets found for {}.".format(scene["id"]))
            return
        vrt_path = os.path.join(
            self._temp_dir, "{}_{}.vrt".format(
                scene["id"].replace("/", "_"), suffix
            )
        )
        self._set_status("Building {} stack ({} bands)..."
                         .format(suffix, len(sources)))
        gdal.UseExceptions()
        try:
            gdal.BuildVRT(
                vrt_path, sources, separate=True, resolution="highest",
                resampleAlg="nearest",
            )
        except Exception as exc:  #noqa: BLE001
            self._set_status("VRT build failed: {}".format(exc))
            return
        name = "{} [{}]".format(scene["id"], suffix)
        layer = QgsRasterLayer(vrt_path, name, "gdal")
        if self._add_layer(layer):
            self._set_status("Loaded {} ({} bands).".format(name, len(sources)))

    #download

    def _on_download(self):
        cfg = self._cfg()
        scenes = self._require_selection()
        if not scenes:
            return

        raw = bool(cfg.get("raw_download"))
        headers = None
        if cfg.get("auth_download") and cfg.get("auth") == "copernicus":
            if not self.cop_auth.configured():
                QMessageBox.warning(
                    self, "SatStream",
                    "This platform downloads from the Copernicus Data Space "
                    "and needs your account.\nEnter and connect your "
                    "credentials below 'Platform Selection' first.",
                )
                return
            try:
                headers = {
                    "Authorization": "Bearer " + self.cop_auth.get_token()
                }
            except AuthError as exc:
                QMessageBox.warning(self, "SatStream", str(exc))
                return

        out_dir = QFileDialog.getExistingDirectory(
            self, "Choose download folder"
        )
        if not out_dir:
            return
        clip = (self._canvas_bbox_4326()
                if self.clip_check.isChecked() and not raw else None)
        if raw and self.clip_check.isChecked():
            self._set_status(
                "Note: whole-product downloads (.zip) cannot be clipped."
            )
        for scene in scenes:
            key, href = stac_client.pick_asset(scene["assets"],
                                               cfg.get("download_assets"))
            if not href:
                #fall back to the first non-metadata asset
                for k, asset in scene["assets"].items():
                    h = asset.get("href", "")
                    if h and not h.endswith((".json", ".xml", ".txt")):
                        key, href = k, h
                        break
            if not href:
                self._set_status("No downloadable asset for {}."
                                 .format(scene["id"]))
                continue
            safe_id = scene["id"].replace("/", "_")
            if raw:
                out_path = os.path.join(out_dir,
                                        "{}.zip".format(safe_id))
                task = DownloadTask(href, out_path, None,
                                    self._download_done,
                                    headers=headers, raw=True)
            else:
                try:
                    signed = stac_client.sign_href(href,
                                                   cfg.get("sign", False))
                except stac_client.StacError as exc:
                    self._set_status(str(exc))
                    continue
                out_path = os.path.join(
                    out_dir, "{}_{}.tif".format(safe_id, key)
                )
                task = DownloadTask(signed, out_path, clip,
                                    self._download_done)
            self._tasks.append(task)
            QgsApplication.taskManager().addTask(task)
            self._set_status("Downloading {} ...".format(out_path))

    def _download_done(self, ok, out_path, error):
        if not ok:
            self._set_status("Download failed: {}".format(error))
            return
        self._set_status("Downloaded {}".format(out_path))
        if out_path.lower().endswith((".tif", ".tiff")):
            layer = QgsRasterLayer(out_path, os.path.basename(out_path),
                                   "gdal")
            self._add_layer(layer)

    #clear

    def _on_clear_layers(self):
        project = QgsProject.instance()
        for layer_id in self._layer_ids:
            if project.mapLayer(layer_id):
                project.removeMapLayer(layer_id)
        self._layer_ids = []
        group = project.layerTreeRoot().findGroup(GROUP_NAME)
        if group is not None and not group.children():
            project.layerTreeRoot().removeChildNode(group)
        self.canvas.refresh()
        self._set_status("SatStream layers cleared.")

    def closeEvent(self, event):
        self.suspend_map()
        super().closeEvent(event)


_ID_PREFIXES = [
    (r"^S2([A-D])_", "sentinel-2{}"),
    (r"^S3([A-D])_", "sentinel-3{}"),
    (r"^S5P_", "sentinel-5p"),
    (r"^L[COTEM]0?(\d)_", "landsat-{}"),
]


def _sensor_key(scene):
    """Return a normalized sensor name such as 'sentinel-2a' or 'landsat-9'.

    Uses the STAC platform property, falling back to the scene ID prefix
    when a catalog leaves the platform blank.
    """
    platform = (scene.get("platform") or "").strip().lower()
    if platform:
        return platform.replace("_", "-").replace(" ", "-")
    scene_id = (scene.get("id") or "").upper()
    for pattern, template in _ID_PREFIXES:
        match = re.match(pattern, scene_id)
        if match:
            groups = match.groups()
            return template.format(groups[0].lower()) if groups else template
    return ""


def _sensor_label(key):
    """Turn 'sentinel-2a' into '2-A', 'sentinel-5p' into '5P' and
    'landsat-9' into 'LS-9'."""
    match = re.match(r"^sentinel-?(\d+)([a-z]*)$", key)
    if match:
        number, letter = match.groups()
        if letter == "p":
            return "{}P".format(number)
        return "{}-{}".format(number, letter.upper()) if letter else number
    match = re.match(r"^landsat-?(\d+)$", key)
    if match:
        return "LS-{}".format(match.group(1))
    return key.upper()
