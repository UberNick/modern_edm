"""
Main Field Recording Window for Modern EDMWin Replacement.
Wires all 10 decompiled edmwin.exe subsystems into the UI:
- Unit-scoped field state save/restore on UNIT change & post-record (EDM_Units carryover)
- Multi-field [BUTTON1]-[BUTTON6] SpeedButton macros with Ctrl+<key> shortcuts & safe UNIT switching
- Multi-shot continuation validation ('No initial record in this series has been recorded') & series lock
- Duplicate ID detection & frmDupID 3-way resolution (Alpha ID, New ID, Continuation +Shot)
- Series-aware point deletion with automatic SUFFIX resequencing (FUN_00489a60)
- Auto-Find Unit (FindUnit spatial lookup), Prompt for Prism, SAPI Speech formatting, and Bag Label spooling
- 6-Way Post-Shot Coordinate Offset (frmOffSet) with cross-unit boundary alert
- 4-Table Legacy EDMWin CSV Bundle Export (_points, _datums, _prisms, _units) & atomic .CFG save
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from drivers.total_station import RawObservation, SerialConfig, TotalStationDriver, create_driver
from storage.cfg_parser import (
    EDMConfig,
    SpeedButtonMacro,
    load_cfg_file,
    save_legacy_cfg_file,
)
from storage.database import EDMDatabase
from storage.label_printer import append_label_to_spool
from survey.math_engine import (
    ExcavationUnit,
    Point3D,
    Prism,
    StationState,
    auto_find_unit,
    check_point_in_unit,
    format_speech_unit_id,
    reduce_polar_to_cartesian,
)
from ui.dialogs import (
    AboutDialog,
    CFGFieldEditorDialog,
    CloudAndEmailShareDialog,
    ContextDefaultsDialog,
    CreateDefaultProjectDialog,
    DataGridDialog,
    DebugStationDialog,
    DuplicateIDDialog,
    EDMStatusDialog,
    EntityManagerDialog,
    FieldFilterDialog,
    HelpViewerDialog,
    LabelPrinterDialog,
    LegacyMigrationDialog,
    ManualShotDialog,
    OffsetPointDialog,
    SerialTerminalDialog,
    SetupLogFilesDialog,
    SpeedButtonEditorDialog,
    StationSetupDialog,
)
from ui.plot_canvas import SurveyPlotCanvas


class EDMMainWindow(QMainWindow):
    def __init__(self, db_path: str | Path, cfg_path: str | Path) -> None:
        super().__init__()
        self.db_path = Path(db_path)
        self.cfg_path = Path(cfg_path)
        self.db = EDMDatabase(self.db_path)
        self.cfg: EDMConfig = load_cfg_file(self.cfg_path)

        # Sync station state if legacy CFG defines StationX/Y/Z and DB is at default
        if (
            self.cfg.station_x is not None
            and self.cfg.station_y is not None
            and self.cfg.station_z is not None
        ):
            curr_stn = self.db.load_station_state()
            if curr_stn.station_name == "STN1" and curr_stn.x == 100.0 and curr_stn.y == 100.0:
                legacy_stn = StationState.from_legacy_cfg(
                    station_name=self.cfg.station_name,
                    station_x=self.cfg.station_x,
                    station_y=self.cfg.station_y,
                    station_z_center=self.cfg.station_z,
                    instrument_height=curr_stn.instrument_height,
                )
                self.db.save_station_state(legacy_stn)

        self.serial_config = SerialConfig(
            port=self.cfg.com_port,
            baudrate=self.cfg.baudrate,
            parity=self.cfg.parity,
            bytesize=self.cfg.bytesize,
            stopbits=self.cfg.stopbits,
            edm_delay_time=self.cfg.edm_delay_time,
        )
        self.driver: TotalStationDriver = create_driver(
            self.cfg.station_type, self.serial_config, log_callback=self._on_driver_serial_log
        )
        self.field_widgets: Dict[str, QWidget] = {}
        self._active_unit_name: str = ""
        self._suppress_unit_hook: bool = False
        self.last_speech_Readout: str = ""
        self.filter_current_station_only: bool = False
        self.active_filter_field: Optional[str] = None
        self.active_filter_value: Optional[str] = None
        self.label_spool_path: Path = self.db_path.parent / "artifact_labels_spool.txt"

        self.setWindowTitle("Modern EDM — Total Station Excavation & Survey Recorder")
        self.resize(1260, 800)
        self._apply_field_stylesheet()
        self._build_menu()
        self._build_ui()
        self._refresh_all()

    def _apply_field_stylesheet(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background-color: #1B222C; color: #F8F9FA; font-size: 13px; }
            QGroupBox { border: 1px solid #3A4659; border-radius: 6px; margin-top: 8px; padding-top: 10px; font-weight: bold; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #4CC9F0; }
            QLineEdit, QComboBox, QPlainTextEdit, QTableWidget {
                background-color: #0F141A; color: #F8F9FA; border: 1px solid #495057; border-radius: 4px; padding: 5px;
            }
            QPushButton {
                background-color: #2B3748; color: #FFFFFF; border: 1px solid #4A5D79;
                border-radius: 5px; padding: 8px 12px; font-weight: bold;
            }
            QPushButton:hover { background-color: #3B4D66; }
            QPushButton#primaryMeasure { background-color: #2A9D8F; border-color: #52B788; font-size: 15px; padding: 12px; }
            QPushButton#primaryMeasure:hover { background-color: #21867A; }
            QPushButton#speedBtn { background-color: #3A506B; border-color: #5BC0BE; }
            """
        )

    def _build_menu(self) -> None:
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&File")
        create_def_act = QAction("&Create Default CFG and MDB...", self)
        create_def_act.triggered.connect(self._open_create_default_project)
        load_cfg_act = QAction("&Open .CFG / JSON Configuration...", self)
        load_cfg_act.triggered.connect(self._on_load_cfg)
        save_cfg_act = QAction("&Save .CFG As...", self)
        save_cfg_act.triggered.connect(self._on_save_cfg)
        exp_bundle_act = QAction("Export 4-Table EDMWin CSV &Bundle (_points, _datums, _prisms, _units)...", self)
        exp_bundle_act.triggered.connect(self._on_export_csv_bundle)
        exp_csv_act = QAction("Export Points to Single CSV...", self)
        exp_csv_act.triggered.connect(self._on_export_csv)
        imp_csv_act = QAction("Import Points from CSV...", self)
        imp_csv_act.triggered.connect(self._on_import_csv)
        exp_geo_act = QAction("Export QGIS GeoJSON (Points + Lines)...", self)
        exp_geo_act.triggered.connect(self._on_export_geojson)
        imp_legacy_act = QAction("&Import Legacy EDMWin Project (.MDB / 4-File CSV Bundle)...", self)
        imp_legacy_act.triggered.connect(self._open_legacy_migration)
        cloud_dbx_act = QAction("Cloud Sync: &Dropbox (Import / Export)...", self)
        cloud_dbx_act.triggered.connect(lambda: self._open_cloud_and_email_share(0))
        cloud_gdrive_act = QAction("Cloud Sync: &Google Drive (Import / Export)...", self)
        cloud_gdrive_act.triggered.connect(lambda: self._open_cloud_and_email_share(1))
        email_share_act = QAction("Share Export via &Email / Gmail Attachment...", self)
        email_share_act.triggered.connect(lambda: self._open_cloud_and_email_share(2))
        print_lbl_dlg_act = QAction("&Print / Reprint Artifact Bag Labels...", self)
        print_lbl_dlg_act.triggered.connect(self._open_label_printer)
        file_menu.addActions([
            create_def_act,
            load_cfg_act,
            save_cfg_act,
            imp_legacy_act,
            cloud_dbx_act,
            cloud_gdrive_act,
            email_share_act,
            print_lbl_dlg_act,
            exp_bundle_act,
            exp_csv_act,
            imp_csv_act,
            exp_geo_act,
        ])

        setup_menu = menubar.addMenu("&Station && Edit")
        edit_fields_act = QAction("Add/Edit &Fields (.CFG)...", self)
        edit_fields_act.triggered.connect(self._open_field_editor)
        edit_btns_act = QAction("Shot &Buttons ([BUTTON1]-[BUTTON6])...", self)
        edit_btns_act.triggered.connect(self._open_button_editor)
        ctx_def_act = QAction("&Context Dependent Defaults...", self)
        ctx_def_act.triggered.connect(self._open_context_defaults)
        manual_shot_act = QAction("&Manual Shot Entry (Angles / XYZ)...", self)
        manual_shot_act.triggered.connect(self._on_manual_shot)
        stn_act = QAction("&Initialize / Verify Station...", self)
        stn_act.triggered.connect(self._open_station_wizard)
        dat_act = QAction("Manage &Datums...", self)
        dat_act.triggered.connect(lambda: self._open_entity_manager("DATUMS"))
        prs_act = QAction("Manage &Prisms...", self)
        prs_act.triggered.connect(lambda: self._open_entity_manager("PRISMS"))
        unt_act = QAction("Manage Excavation &Units...", self)
        unt_act.triggered.connect(lambda: self._open_entity_manager("UNITS"))
        offset_act = QAction("&Offset Last Point (6-Way)...", self)
        offset_act.triggered.connect(self._on_offset_last_point)
        com_act = QAction("&Select Total Station && COM Settings...", self)
        com_act.triggered.connect(self._open_serial_terminal)
        debug_ts_act = QAction("&Debug Total Station...", self)
        debug_ts_act.triggered.connect(self._open_debug_station)
        setup_logs_act = QAction("Setup &Log Files...", self)
        setup_logs_act.triggered.connect(self._open_setup_log_files)
        reset_conn_act = QAction("&Reset Connection", self)
        reset_conn_act.triggered.connect(self._on_reset_connection)
        setup_menu.addActions([
            edit_fields_act,
            edit_btns_act,
            ctx_def_act,
            manual_shot_act,
            stn_act,
            dat_act,
            prs_act,
            unt_act,
            offset_act,
            com_act,
            debug_ts_act,
            setup_logs_act,
            reset_conn_act,
        ])

        options_menu = menubar.addMenu("&Options")
        self.auto_find_act = QAction("&Auto-Find Unit", self, checkable=True)
        self.auto_find_act.setChecked(self.cfg.auto_find_unit)
        self.auto_find_act.toggled.connect(self._on_toggle_options)

        self.prompt_prism_act = QAction("&Prompt for Prism", self, checkable=True)
        self.prompt_prism_act.setChecked(self.cfg.prompt_prism)
        self.prompt_prism_act.toggled.connect(self._on_toggle_options)

        self.print_labels_act = QAction("P&rint Shots (Artifact Bag Labels)", self, checkable=True)
        self.print_labels_act.setChecked(self.cfg.print_labels)
        self.print_labels_act.toggled.connect(self._on_toggle_options)

        self.speech_act = QAction("&Speak Unit-ID on New Shots", self, checkable=True)
        self.speech_act.setChecked(self.cfg.speech_enabled)
        self.speech_act.toggled.connect(self._on_toggle_options)

        self.uppercase_act = QAction("&UpperCase All Entries", self, checkable=True)
        self.uppercase_act.setChecked(self.cfg.upper_case)
        self.uppercase_act.toggled.connect(self._on_toggle_options)

        self.show_datagrid_act = QAction("Show &Data Grid...", self)
        self.show_datagrid_act.triggered.connect(self._open_data_grid)

        self.filter_grid_act = QAction("Set Grid &Filter (Field && Value)...", self)
        self.filter_grid_act.triggered.connect(self._open_field_filter)

        self.filter_station_act = QAction("Show only Points from Current &Station", self, checkable=True)
        self.filter_station_act.setChecked(self.filter_current_station_only)
        self.filter_station_act.toggled.connect(self._on_toggle_options)

        options_menu.addActions([
            self.auto_find_act,
            self.prompt_prism_act,
            self.print_labels_act,
            self.speech_act,
            self.uppercase_act,
            self.show_datagrid_act,
            self.filter_grid_act,
            self.filter_station_act,
        ])

        help_menu = menubar.addMenu("&Help")
        help_topics_act = QAction("&Help Topics [F1]...", self)
        help_topics_act.setShortcut("F1")
        help_topics_act.triggered.connect(self._open_help_dialog)
        status_act = QAction("&Status...", self)
        status_act.triggered.connect(self._open_status_dialog)
        about_act = QAction("&About...", self)
        about_act.triggered.connect(self._open_about_dialog)
        help_menu.addActions([help_topics_act, status_act, about_act])

    def _on_toggle_options(self) -> None:
        self.cfg.auto_find_unit = self.auto_find_act.isChecked()
        self.cfg.prompt_prism = self.prompt_prism_act.isChecked()
        self.cfg.print_labels = self.print_labels_act.isChecked()
        self.cfg.speech_enabled = self.speech_act.isChecked()
        self.cfg.upper_case = self.uppercase_act.isChecked()
        self.filter_current_station_only = self.filter_station_act.isChecked()
        self._refresh_all()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)

        banner_box = QGroupBox("Active Station & Instrument Status")
        banner_layout = QHBoxLayout(banner_box)
        self.station_lbl = QLabel()
        self.station_lbl.setFont(QFont("Consolas", 11, QFont.Bold))

        self.prism_combo = QComboBox()
        self.prism_combo.setMinimumWidth(240)

        init_btn = QPushButton("Initialize / Verify Station")
        init_btn.clicked.connect(self._open_station_wizard)
        com_btn = QPushButton("Test COM")
        com_btn.clicked.connect(self._open_serial_terminal)

        banner_layout.addWidget(self.station_lbl, 1)
        banner_layout.addWidget(QLabel("Active Prism:"))
        banner_layout.addWidget(self.prism_combo)
        banner_layout.addWidget(init_btn)
        banner_layout.addWidget(com_btn)
        root_layout.addWidget(banner_box)

        splitter = QSplitter(Qt.Horizontal)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)

        self.form_group = QGroupBox("Field Data Entry (.CFG)")
        self.dynamic_form = QFormLayout(self.form_group)
        self._rebuild_cfg_form()
        left_layout.addWidget(self.form_group)

        self.speed_group = QGroupBox("Speedbuttons (1-Tap Code / Multi-Field Macro & Measure)")
        self.speed_layout = QHBoxLayout(self.speed_group)
        self._rebuild_speedbuttons()
        left_layout.addWidget(self.speed_group)

        self.multi_shot_chk = QCheckBox("Continue Multi-Shot Sequence (+Shot: Keep ID, Increment SUFFIX)")
        left_layout.addWidget(self.multi_shot_chk)

        meas_rec_btn = QPushButton("MEASURE && RECORD SHOT [F2]")
        meas_rec_btn.setObjectName("primaryMeasure")
        meas_rec_btn.setShortcut("F2")
        meas_rec_btn.clicked.connect(self._on_measure_and_record)
        left_layout.addWidget(meas_rec_btn)

        sec_row = QHBoxLayout()
        meas_only_btn = QPushButton("X-Shot (Measure Only)")
        meas_only_btn.clicked.connect(self._on_measure_only)
        man_shot_btn = QPushButton("Manual Shot")
        man_shot_btn.clicked.connect(self._on_manual_shot)
        offset_btn = QPushButton("Offset Last Pt")
        offset_btn.clicked.connect(self._on_offset_last_point)
        edit_last_btn = QPushButton("Save Edits to Last Pt")
        edit_last_btn.clicked.connect(self._on_edit_last_point)
        del_last_btn = QPushButton("Delete Obj")
        del_last_btn.clicked.connect(self._on_delete_last_point)
        sec_row.addWidget(meas_only_btn)
        sec_row.addWidget(man_shot_btn)
        sec_row.addWidget(offset_btn)
        sec_row.addWidget(edit_last_btn)
        sec_row.addWidget(del_last_btn)
        left_layout.addLayout(sec_row)

        self.last_obs_lbl = QLabel("Last Shot: None")
        self.last_obs_lbl.setFont(QFont("Consolas", 11))
        left_layout.addWidget(self.last_obs_lbl)
        left_layout.addStretch()
        splitter.addWidget(left_widget)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)

        plot_header = QHBoxLayout()
        plot_header.addWidget(QLabel("Live Survey Plot:"))
        for mode_label, mode_code in [("Plan (X-Y)", "XY"), ("Front (X-Z)", "XZ"), ("Side (Y-Z)", "YZ")]:
            b = QPushButton(mode_label)
            b.clicked.connect(lambda _, m=mode_code: self.canvas.set_view_mode(m))
            plot_header.addWidget(b)
        plot_header.addStretch()
        datagrid_btn = QPushButton("Show Data Grid")
        datagrid_btn.clicked.connect(self._open_data_grid)
        filter_btn = QPushButton("Filter by ...")
        filter_btn.clicked.connect(self._open_field_filter)
        plot_header.addWidget(datagrid_btn)
        plot_header.addWidget(filter_btn)
        right_layout.addLayout(plot_header)

        self.canvas = SurveyPlotCanvas()
        right_layout.addWidget(self.canvas, 1)

        self.points_table = QTableWidget()
        self.points_table.setColumnCount(7)
        self.points_table.setHorizontalHeaderLabels(["#", "UNIT", "ID", "SUFFIX", "X", "Y", "Z"])
        self.points_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.points_table.setMaximumHeight(220)
        right_layout.addWidget(self.points_table)

        splitter.addWidget(right_widget)
        splitter.setSizes([520, 740])
        root_layout.addWidget(splitter, 1)

    def _rebuild_cfg_form(self) -> None:
        while self.dynamic_form.rowCount() > 0:
            self.dynamic_form.removeRow(0)
        self.field_widgets.clear()

        unit_names = [u.name for u in self.db.list_units()]
        for f in self.cfg.fields:
            label_text = f.prompt if f.prompt else f.name
            if f.name.upper() == "UNIT":
                cb = QComboBox()
                cb.setEditable(True)
                merged = list(dict.fromkeys(unit_names + f.menu_items))
                cb.addItems(merged)
                if f.default:
                    cb.setCurrentText(f.default)
                self._active_unit_name = cb.currentText().strip()
                cb.currentTextChanged.connect(self._on_unit_changed)
                self.field_widgets[f.name] = cb
                self.dynamic_form.addRow(f"{label_text}:", cb)
            elif f.field_type == "MENU" or f.menu_items:
                cb = QComboBox()
                cb.setEditable(True)
                cb.addItems(f.menu_items)
                cb.setCurrentText(f.default)
                cb.currentTextChanged.connect(
                    lambda val, fname=f.name: self._on_master_field_changed(fname, val)
                )
                self.field_widgets[f.name] = cb
                self.dynamic_form.addRow(f"{label_text}:", cb)
            else:
                le = QLineEdit(f.default)
                if f.length > 0:
                    le.setMaxLength(f.length)
                le.textChanged.connect(
                    lambda val, fname=f.name: self._on_master_field_changed(fname, val)
                )
                self.field_widgets[f.name] = le
                self.dynamic_form.addRow(f"{label_text}:", le)

        # Restore initial unit's saved fields if present
        if self._active_unit_name:
            saved_state = self.db.load_unit_field_state(self._active_unit_name)
            if saved_state:
                self._suppress_unit_hook = True
                try:
                    self._set_form_values(saved_state)
                finally:
                    self._suppress_unit_hook = False

    def _on_unit_changed(self, new_unit_text: str) -> None:
        """
        Replicates FillUnitFields & UpdateUnitTable in edmwin.exe (FUN_0048afb0):
        1. Blocks changing UNIT in the middle of a continuation (+Shot) series (0x0042d48c).
        2. Saves unit-scoped fields (ID, LEVEL, EXCAVATOR) for the previous unit.
        3. Restores unit-scoped fields for the newly selected unit.
        """
        if self._suppress_unit_hook:
            return
        new_unit = new_unit_text.strip()
        if not new_unit or new_unit.upper() == self._active_unit_name.upper():
            return

        # Check mid-series unit lock (0x0042d48c: 'You cannot change unit within a series')
        curr_vals = self._get_form_values()
        curr_suffix = int(curr_vals.get("SUFFIX", "0") or 0)
        if self.multi_shot_chk.isChecked() or curr_suffix > 0:
            QMessageBox.warning(
                self,
                "Series Unit Lock",
                "You cannot change unit within a series",
            )
            self._suppress_unit_hook = True
            try:
                self._set_form_values({"UNIT": self._active_unit_name})
            finally:
                self._suppress_unit_hook = False
            return

        scoped_names = self.cfg.get_unit_scoped_field_names()
        if self._active_unit_name and scoped_names:
            old_state = {
                k: curr_vals[k] for k in scoped_names if k in curr_vals
            }
            self.db.save_unit_field_state(self._active_unit_name, old_state)

        self._active_unit_name = new_unit
        restored = self.db.load_unit_field_state(new_unit)
        if restored:
            self._suppress_unit_hook = True
            try:
                self._set_form_values(restored)
            finally:
                self._suppress_unit_hook = False
        else:
            # Look up last numeric ID in database for this unit + 1
            max_id = self.db.get_max_numeric_id_for_unit(new_unit)
            if max_id > 0:
                self._set_form_values({"ID": str(max_id + 1), "SUFFIX": "0"})

    def _save_current_unit_scoped_state(self, values: Dict[str, Any]) -> None:
        unit_name = str(values.get("UNIT", "")).strip()
        if not unit_name:
            return
        scoped_names = self.cfg.get_unit_scoped_field_names()
        if not scoped_names:
            return
        state_to_save = {k: str(values[k]) for k in scoped_names if k in values}
        if state_to_save:
            self.db.save_unit_field_state(unit_name, state_to_save)

    def _rebuild_speedbuttons(self) -> None:
        while self.speed_layout.count():
            item = self.speed_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if self.cfg.button_macros:
            for macro in self.cfg.button_macros:
                label = f"{macro.title} (Ctrl+{macro.shortcut})" if macro.shortcut else macro.title
                btn = QPushButton(label)
                btn.setObjectName("speedBtn")
                if macro.shortcut:
                    btn.setShortcut(f"Ctrl+{macro.shortcut}")
                btn.clicked.connect(lambda _, m=macro: self._on_speedbutton_macro(m))
                self.speed_layout.addWidget(btn)
        else:
            for code in self.cfg.speed_buttons:
                btn = QPushButton(code)
                btn.setObjectName("speedBtn")
                btn.clicked.connect(lambda _, c=code: self._on_speedbutton(c))
                self.speed_layout.addWidget(btn)

    def _get_form_values(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for name, widget in self.field_widgets.items():
            if isinstance(widget, QComboBox):
                val = widget.currentText().strip()
            elif isinstance(widget, QLineEdit):
                val = widget.text().strip()
            else:
                val = ""
            if self.cfg.upper_case:
                val = val.upper()
            out[name] = val
        return out

    def _set_form_values(self, values: Dict[str, Any]) -> None:
        for name, val in values.items():
            w = self.field_widgets.get(name)
            if w is None:
                for k, cand in self.field_widgets.items():
                    if k.upper() == name.upper():
                        w = cand
                        break
            if isinstance(w, QComboBox):
                w.setCurrentText(str(val))
            elif isinstance(w, QLineEdit):
                w.setText(str(val))

    def _on_speedbutton_macro(self, macro: SpeedButtonMacro) -> None:
        """
        Replicates FUN_0045fc90 (Button_Click):
        Applies all field presets in the SpeedButtonMacro (including PRISM or UNIT switches)
        and triggers measurement & recording.
        """
        curr = self._get_form_values()
        self._save_current_unit_scoped_state(curr)

        updated = self.cfg.apply_button_macro(
            macro, curr, unit_state_lookup_fn=self.db.load_unit_field_state
        )

        for fk, fv in macro.field_values.items():
            if fk.upper() == "PRISM":
                for i in range(self.prism_combo.count()):
                    p = self.prism_combo.itemData(i)
                    if isinstance(p, Prism) and p.name.upper() == fv.strip().upper():
                        self.prism_combo.setCurrentIndex(i)
                        break

        self._suppress_unit_hook = True
        try:
            self._set_form_values(updated)
            if "UNIT" in updated:
                self._active_unit_name = str(updated["UNIT"]).strip()
        finally:
            self._suppress_unit_hook = False

        self._on_measure_and_record()

    def _on_speedbutton(self, code_value: str) -> None:
        macro = SpeedButtonMacro(index=1, title=code_value, field_values={"CODE": code_value})
        self._on_speedbutton_macro(macro)

    def _active_prism(self) -> Prism:
        p = self.prism_combo.currentData()
        return p if isinstance(p, Prism) else Prism("Default", 0.0, 0.0)

    def _find_unit(self, name: str) -> Optional[ExcavationUnit]:
        for u in self.db.list_units():
            if u.name.upper() == name.strip().upper():
                return u
        return None

    def _on_measure_only(self) -> None:
        try:
            stn = self.db.load_station_state()
            prism = self._active_prism()
            obs = self.driver.measure()
            if obs.direct_xyz is not None:
                pt = Point3D(*obs.direct_xyz)
                hd = obs.slope_distance
            else:
                pt, hd, _ = reduce_polar_to_cartesian(
                    station=stn,
                    slope_distance=obs.slope_distance,
                    horizontal_angle_rad=obs.horizontal_angle_rad,
                    vertical_angle_rad=obs.vertical_angle_rad,
                    prism_height=prism.height,
                    prism_offset=prism.offset,
                )
            self.last_obs_lbl.setText(
                f"X-SHOT CHECK -> X={pt.x:.4f} Y={pt.y:.4f} Z={pt.z:.4f} "
                f"(HD={hd:.3f}m, Hz={obs.horizontal_angle_deg:.3f}°, V={obs.vertical_angle_deg:.3f}°)"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Measure Failed", str(exc))

    def _on_measure_and_record(
        self,
        _checked: bool = False,
        manual_obs: Optional[RawObservation] = None,
        manual_prism_height: Optional[float] = None,
    ) -> None:
        try:
            values = self._get_form_values()
            unit_name = values.get("UNIT", "")
            pt_id = values.get("ID", "1")
            is_continuation = self.multi_shot_chk.isChecked()
            suffix_val = int(values.get("SUFFIX", "0") or 0)

            # 1. Multi-shot continuation validation (0x0042c274 & 0x0042c364)
            if is_continuation or suffix_val > 0:
                if not unit_name or not pt_id:
                    QMessageBox.warning(
                        self,
                        "Invalid Continuation",
                        "You cannot continue with an object unless it has a valid Unit and ID.",
                    )
                    return
                if not self.db.has_initial_series_shot(unit_name, pt_id):
                    ans = QMessageBox.question(
                        self,
                        "No Initial Record",
                        "No initial record in this series has been recorded. Shoot as new object?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.Yes,
                    )
                    if ans != QMessageBox.Yes:
                        return
                    is_continuation = False
                    suffix_val = 0
                    self.multi_shot_chk.setChecked(False)
                    values["SUFFIX"] = "0"
                elif suffix_val == 0:
                    _, suffix_val = self.db.resolve_duplicate_id(unit_name, pt_id, "CONTINUE")
                    values["SUFFIX"] = str(suffix_val)

            # 2. Duplicate ID detection & frmDupID resolution (0x004399e8)
            if not is_continuation and self.db.check_duplicate_id(unit_name, pt_id, suffix_val):
                next_num = self.db.get_max_numeric_id_for_unit(unit_name) + 1
                dup_dlg = DuplicateIDDialog(unit_name, pt_id, next_num, self)
                if dup_dlg.exec() != DuplicateIDDialog.Accepted:
                    return
                pt_id, suffix_val = self.db.resolve_duplicate_id(
                    unit_name, pt_id, dup_dlg.selected_option
                )
                values["ID"] = pt_id
                values["SUFFIX"] = str(suffix_val)
                if dup_dlg.selected_option == "CONTINUE":
                    is_continuation = True

            # 3. Prompt for Prism if enabled (0x0042d56c: 'Verify Prism. Currently set to ...')
            prism = self._active_prism()
            if self.cfg.prompt_prism and manual_obs is None:
                prisms = self.db.list_prisms()
                prism_names = [p.name for p in prisms]
                if prism_names:
                    curr_idx = next(
                        (i for i, p in enumerate(prisms) if p.name == prism.name), 0
                    )
                    chosen, ok = QInputDialog.getItem(
                        self,
                        "Select Prism",
                        f"Verify Prism. Currently set to {prism.name}:",
                        prism_names,
                        curr_idx,
                        False,
                    )
                    if not ok:
                        return
                    for i, p in enumerate(prisms):
                        if p.name == chosen:
                            prism = p
                            self.prism_combo.setCurrentIndex(i)
                            break

            if manual_prism_height is not None:
                prism = Prism(prism.name, float(manual_prism_height), prism.offset, prism.description)

            # 4. Trigger measurement (or open ManualShotDialog if station_type is MANUAL/NONE)
            stn = self.db.load_station_state()
            if manual_obs is not None:
                obs = manual_obs
            elif self.cfg.station_type.strip().upper() in ("MANUAL", "NONE"):
                man_dlg = ManualShotDialog(default_prism_height=prism.height, parent=self)
                if man_dlg.exec() != ManualShotDialog.Accepted or man_dlg.observation is None:
                    return
                obs = man_dlg.observation
                prism = Prism(prism.name, float(man_dlg.prism_height), prism.offset, prism.description)
            else:
                obs = self.driver.measure()

            if obs.direct_xyz is not None:
                pt = Point3D(*obs.direct_xyz)
            else:
                pt, _, _ = reduce_polar_to_cartesian(
                    station=stn,
                    slope_distance=obs.slope_distance,
                    horizontal_angle_rad=obs.horizontal_angle_rad,
                    vertical_angle_rad=obs.vertical_angle_rad,
                    prism_height=prism.height,
                    prism_offset=prism.offset,
                )

            # 5. Auto-Find Unit if enabled (mnuFindUnit / FindUnit)
            if self.cfg.auto_find_unit and not is_continuation:
                matched_unit = auto_find_unit(pt, self.db.list_units())
                if matched_unit is not None and matched_unit.name.upper() != unit_name.upper():
                    self._on_unit_changed(matched_unit.name)
                    values = self._get_form_values()
                    values["UNIT"] = matched_unit.name
                    unit_name = matched_unit.name
                    pt_id = values.get("ID", pt_id)

            # 6. Unit boundary limit checking
            if self.cfg.limit_checking:
                unit_obj = self._find_unit(unit_name)
                if unit_obj is not None:
                    ok, msg = check_point_in_unit(pt, unit_obj)
                    if not ok:
                        ans = QMessageBox.warning(
                            self,
                            "Out-of-Bounds Unit Warning",
                            f"{msg}\n\nDo you still want to record this shot?",
                            QMessageBox.Yes | QMessageBox.No,
                            QMessageBox.No,
                        )
                        if ans != QMessageBox.Yes:
                            return

            # 7. Record point to database
            doc_id = self.db.record_point(
                unit=unit_name,
                point_id=pt_id,
                suffix=suffix_val,
                x=pt.x,
                y=pt.y,
                z=pt.z,
                prism=prism,
                slope_dist=obs.slope_distance,
                h_angle_deg=obs.horizontal_angle_deg,
                v_angle_deg=obs.vertical_angle_deg,
                station=stn,
                extra_fields=values,
            )

            # 8. Optional Artifact Bag Label printing (modPrint)
            recorded_pt = self.db.get_last_point()
            if self.cfg.print_labels and recorded_pt is not None:
                append_label_to_spool(
                    recorded_pt,
                    self.label_spool_path,
                    site_name=self.cfg.point_table or "EXCAVATION",
                    use_zpl=False,
                )

            # 9. Optional SAPI Speech readout formatting (FUN_004c2b70)
            speech_suffix = ""
            if self.cfg.speech_enabled:
                self.last_speech_Readout = format_speech_unit_id(unit_name, pt_id)
                speech_suffix = f" | TTS: '{self.last_speech_Readout}'"

            self.last_obs_lbl.setText(
                f"RECORDED #{doc_id} ({unit_name} {pt_id}.{suffix_val}) -> "
                f"X={pt.x:.4f}  Y={pt.y:.4f}  Z={pt.z:.4f}{speech_suffix}"
            )

            # 10. Advance Carry / Increment fields, update Context Defaults, and persist Unit-Scoped state
            ctx_cfg = self.db.load_context_defaults()
            if ctx_cfg["master_var"]:
                for k, v in values.items():
                    if k.upper() == ctx_cfg["master_var"].upper():
                        self.db.update_context_default_rule(v, values)
                        break

            next_vals = self.cfg.apply_post_record_rules(
                values, continue_multi_shot_sequence=is_continuation
            )
            self._suppress_unit_hook = True
            try:
                self._set_form_values(next_vals)
            finally:
                self._suppress_unit_hook = False

            self._save_current_unit_scoped_state(next_vals)
            self._refresh_all()
        except Exception as exc:
            QMessageBox.critical(self, "Measure & Record Error", str(exc))

    def _on_offset_last_point(self) -> None:
        """
        Replicates frmOffSet (0x0043946c & 0x0042d910):
        Applies a 6-directional offset to the last recorded point and checks
        whether the offset moved the point into a different excavation unit.
        """
        last_pt = self.db.get_last_point()
        if not last_pt:
            QMessageBox.information(self, "No Points", "No recorded points to offset.")
            return
        curr_p3d = Point3D(float(last_pt["x"]), float(last_pt["y"]), float(last_pt["z"]))
        dlg = OffsetPointDialog(curr_p3d, self)
        if dlg.exec() == OffsetPointDialog.Accepted:
            new_p = dlg.updated_point
            self.db.update_point_coords(last_pt["doc_id"], new_p.x, new_p.y, new_p.z)

            new_unit = auto_find_unit(new_p, self.db.list_units())
            unit_note = ""
            if new_unit and new_unit.name.upper() != str(last_pt["unit"]).upper():
                unit_note = f" (Moved into unit {new_unit.name})"
                QMessageBox.information(
                    self,
                    "Unit Boundary Crossed",
                    f"Because of the offset, the location of this point is now in unit {new_unit.name}. "
                    f"Change Unit field from pull-down menu",
                )

            self.last_obs_lbl.setText(
                f"OFFSET Point #{last_pt['doc_id']} -> X={new_p.x:.4f} Y={new_p.y:.4f} Z={new_p.z:.4f}{unit_note}"
            )
            self._refresh_all()

    def _on_edit_last_point(self) -> None:
        last_pt = self.db.get_last_point()
        if not last_pt:
            QMessageBox.information(self, "No Points", "No recorded points to update.")
            return
        values = self._get_form_values()
        unit_name = values.get("UNIT", last_pt["unit"])
        pt_id = values.get("ID", last_pt["id"])
        suffix_val = int(values.get("SUFFIX", last_pt["suffix"]) or 0)
        self.db.update_point_fields(last_pt["doc_id"], unit_name, pt_id, suffix_val, values)
        self._save_current_unit_scoped_state(values)
        self.last_obs_lbl.setText(f"Updated Point #{last_pt['doc_id']} attributes.")
        self._refresh_all()

    def _on_delete_last_point(self) -> None:
        """
        Replicates FUN_00489a60 (DeleteRecord):
        Deletes the last point and automatically resequences any subsequent continuation shots.
        """
        last_pt = self.db.get_last_point()
        if not last_pt:
            return
        ans = QMessageBox.question(
            self,
            "Delete Object",
            f"Warning: Deleting will permanently remove Point #{last_pt['doc_id']} "
            f"({last_pt['unit']} ID={last_pt['id']}.{last_pt['suffix']}). Continue anyway?",
        )
        if ans == QMessageBox.Yes:
            res = self.db.delete_point_with_resequencing(
                last_pt["doc_id"], delete_entire_series=False
            )
            reseq = res.get("resequenced_count", 0)
            reseq_msg = f" ({reseq} continuation shot(s) resequenced)" if reseq > 0 else ""
            self.last_obs_lbl.setText(f"Deleted Point #{last_pt['doc_id']}{reseq_msg}.")
            self._refresh_all()

    def _on_reset_connection(self) -> None:
        """Replicates Station -> Reset Connection (0x00437408)."""
        try:
            self.driver.reset_connection()
            QMessageBox.information(
                self,
                "Connection Reset",
                "The connection is reset. Use this option when the computer powers off while the program is running.",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Reset Connection Error", str(exc))

    def _refresh_all(self) -> None:
        stn = self.db.load_station_state()
        self.station_lbl.setText(
            f"STN: {stn.station_name} [{stn.setup_method}] | "
            f"X={stn.x:.3f}  Y={stn.y:.3f}  Z={stn.z:.3f} (CtrZ={stn.instrument_center_z:.3f}) | HI={stn.instrument_height:.3f}m"
        )

        curr_prism_name = self.prism_combo.currentText()
        self.prism_combo.clear()
        for p in self.db.list_prisms():
            self.prism_combo.addItem(f"{p.name} (HT={p.height:.2f}m)", p)
        if curr_prism_name:
            idx = self.prism_combo.findText(curr_prism_name)
            if idx >= 0:
                self.prism_combo.setCurrentIndex(idx)

        stn_xyz = (stn.x, stn.y, stn.instrument_center_z) if self.filter_current_station_only else None
        pts = self.db.filter_points(
            field_name=self.active_filter_field,
            field_value=self.active_filter_value,
            station_xyz=stn_xyz,
        )
        self.points_table.setRowCount(len(pts))
        for r, p in enumerate(pts):
            row_vals = [
                str(p["doc_id"]), p["unit"], str(p["id"]), str(p["suffix"]),
                f"{p['x']:.4f}", f"{p['y']:.4f}", f"{p['z']:.4f}",
            ]
            for c, val in enumerate(row_vals):
                self.points_table.setItem(r, c, QTableWidgetItem(val))
        self.points_table.scrollToBottom()

        self.canvas.set_data(
            points=pts,
            datums=self.db.list_datums(),
            units=self.db.list_units(),
            station=stn,
        )

    def _open_data_grid(self) -> None:
        """Opens the full editable DataGrid dialog (frmDataGrid replica)."""
        dlg = DataGridDialog(
            self.db,
            self.cfg,
            filter_current_station_only=self.filter_current_station_only,
            parent=self,
        )
        if self.active_filter_field and self.active_filter_value is not None:
            dlg.apply_filter(self.active_filter_field, self.active_filter_value)
        dlg.exec()
        self.filter_current_station_only = dlg.filter_current_station_only
        self.filter_station_act.setChecked(self.filter_current_station_only)
        self.active_filter_field = dlg.active_filter_field
        self.active_filter_value = dlg.active_filter_value
        self._refresh_all()

    def _open_field_filter(self) -> None:
        """Opens the Field & Distinct Value filter dialog (frmFilter replica)."""
        dlg = FieldFilterDialog(self.db, self.cfg, self)
        if dlg.exec() == FieldFilterDialog.Accepted:
            self.active_filter_field = dlg.selected_field
            self.active_filter_value = dlg.selected_value
            self._refresh_all()

    def _open_context_defaults(self) -> None:
        """Opens Context-Dependent Defaults (frmSubUnits replica)."""
        dlg = ContextDefaultsDialog(self.db, self.cfg, self)
        dlg.exec()

    def _on_driver_serial_log(self, direction: str, payload: str) -> None:
        """Writes total station serial traffic to TSLogFile when UseTSLog is enabled."""
        if self.cfg.use_ts_log and self.cfg.ts_log_file:
            SetupLogFilesDialog.append_log_entry(self.cfg.ts_log_file, f"{direction} {payload}")

    def _open_setup_log_files(self) -> None:
        """Opens Setup Log Files (frmSetupLogFiles replica)."""
        dlg = SetupLogFilesDialog(self.cfg, default_dir=self.cfg_path.parent, parent=self)
        if dlg.exec() == SetupLogFilesDialog.Accepted:
            if self.cfg.use_general_log and self.cfg.general_log_file:
                SetupLogFilesDialog.append_log_entry(
                    self.cfg.general_log_file,
                    f"Updated log file settings (UseTSLog={self.cfg.use_ts_log}, UseGeneralLog={self.cfg.use_general_log})",
                )
            if self.cfg_path.suffix.lower() == ".cfg" and self.cfg_path.exists():
                save_legacy_cfg_file(self.cfg, self.cfg_path)

    def _open_create_default_project(self) -> None:
        """Opens Create Default CFG and MDB (frmDefault replica) and loads the created project."""
        dlg = CreateDefaultProjectDialog(default_dir=self.cfg_path.parent, parent=self)
        if dlg.exec() == CreateDefaultProjectDialog.Accepted and dlg.created_cfg_path and dlg.created_db_path:
            self.db.close()
            self.cfg_path = dlg.created_cfg_path
            self.db_path = dlg.created_db_path
            self.db = EDMDatabase(self.db_path)
            self.cfg = load_cfg_file(self.cfg_path)
            self._rebuild_cfg_form()
            self._rebuild_speedbuttons()
            self._refresh_all()

    def _open_debug_station(self) -> None:
        """Opens Debug Total Station (frmDebug replica)."""
        dlg = DebugStationDialog(self.db, self.cfg, self.driver, self)
        dlg.exec()

    def _open_status_dialog(self) -> None:
        """Opens EDM Status diagnostic summary (frmStatus replica)."""
        dlg = EDMStatusDialog(
            self.db,
            self.cfg,
            cfg_path=str(self.cfg_path),
            ini_path=str(self.cfg_path.parent / "edm.ini"),
            parent=self,
        )
        dlg.exec()

    def _open_about_dialog(self) -> None:
        """Opens About EDM (frmAbout replica)."""
        dlg = AboutDialog(self)
        dlg.exec()

    def _open_label_printer(self) -> None:
        """Opens the Artifact Bag Label Printer & Reprint dialog."""
        dlg = LabelPrinterDialog(self.db, self.cfg, self.label_spool_path, self)
        dlg.exec()
        self.label_spool_path = dlg.spool_path

    def _open_legacy_migration(self) -> None:
        """Opens the Legacy EDMWin (.MDB / 4-File CSV Bundle) migration dialog."""
        dlg = LegacyMigrationDialog(self.db, default_dir=self.cfg_path.parent, parent=self)
        dlg.exec()
        if dlg.last_report is not None:
            self._rebuild_cfg_form()
            self._refresh_all()

    def _open_cloud_and_email_share(self, initial_tab: int = 0) -> None:
        """Opens the Dropbox / Google Drive Sync & Email / Gmail Attachment Share dialog."""
        dlg = CloudAndEmailShareDialog(
            self.db,
            self.cfg,
            cfg_path=self.cfg_path,
            initial_tab=initial_tab,
            parent=self,
        )
        dlg.exec()
        self._rebuild_cfg_form()
        self._refresh_all()

    def _open_help_dialog(self) -> None:
        """Opens the in-app Help Topics viewer (frmHelp replica)."""
        dlg = HelpViewerDialog(self)
        dlg.exec()

    def _on_master_field_changed(self, field_name: str, new_value: str) -> None:
        """
        Replicates FillDependentFields (0x0042b12c):
        When the user changes the Master Variable on the main form, fills in
        the saved Dependent Variable defaults for that Master Value.
        """
        if self._suppress_unit_hook:
            return
        ctx = self.db.load_context_defaults()
        if not ctx["master_var"] or ctx["master_var"].upper() != field_name.strip().upper():
            return
        dep_vals = self.db.get_context_dependent_values(new_value)
        if dep_vals:
            self._suppress_unit_hook = True
            try:
                self._set_form_values(dep_vals)
            finally:
                self._suppress_unit_hook = False

    def _on_manual_shot(self) -> None:
        """Opens ManualShotDialog (frmManualshot replica) and records the point."""
        prism = self._active_prism()
        dlg = ManualShotDialog(default_prism_height=prism.height, parent=self)
        if dlg.exec() == ManualShotDialog.Accepted and dlg.observation is not None:
            self._on_measure_and_record(
                manual_obs=dlg.observation,
                manual_prism_height=dlg.prism_height,
            )

    def _open_field_editor(self) -> None:
        """Opens the visual .CFG Field Editor (frmEditField replica)."""
        dlg = CFGFieldEditorDialog(self.cfg, self)
        if dlg.exec() == CFGFieldEditorDialog.Accepted:
            self._rebuild_cfg_form()
            if self.cfg_path.suffix.lower() == ".cfg" and self.cfg_path.exists():
                save_legacy_cfg_file(self.cfg, self.cfg_path)

    def _open_button_editor(self) -> None:
        """Opens the visual 6-Slot SpeedButton Macro Editor (frmButtons replica)."""
        dlg = SpeedButtonEditorDialog(
            self.cfg,
            prisms=self.db.list_prisms(),
            units=self.db.list_units(),
            parent=self,
        )
        if dlg.exec() == SpeedButtonEditorDialog.Accepted:
            self._rebuild_speedbuttons()
            if self.cfg_path.suffix.lower() == ".cfg" and self.cfg_path.exists():
                save_legacy_cfg_file(self.cfg, self.cfg_path)

    def _open_station_wizard(self) -> None:
        dlg = StationSetupDialog(self.db, self.driver, self)
        dlg.exec()
        self._refresh_all()

    def _open_entity_manager(self, mode: str) -> None:
        dlg = EntityManagerDialog(self.db, mode, self)
        dlg.exec()
        self._rebuild_cfg_form()
        self._refresh_all()

    def _open_serial_terminal(self) -> None:
        dlg = SerialTerminalDialog(self.cfg.station_type, self.serial_config, self)
        if dlg.exec() == SerialTerminalDialog.Accepted:
            st_type, new_cfg = dlg.build_config()
            self.cfg.station_type = st_type
            self.serial_config = new_cfg
            self.driver.disconnect()
            self.driver = create_driver(self.cfg.station_type, self.serial_config)

    def _on_load_cfg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open EDM .CFG or JSON", "", "Config Files (*.cfg *.CFG *.json)"
        )
        if path:
            self.cfg_path = Path(path)
            self.cfg = load_cfg_file(path)
            self._rebuild_cfg_form()
            self._rebuild_speedbuttons()

    def _on_save_cfg(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CFG file as ....", str(self.cfg_path), "CFG Files (*.cfg)"
        )
        if path:
            stn = self.db.load_station_state()
            self.cfg.station_name = stn.station_name
            self.cfg.station_x = stn.x
            self.cfg.station_y = stn.y
            self.cfg.station_z = stn.instrument_center_z
            save_legacy_cfg_file(self.cfg, path)
            QMessageBox.information(self, "Saved CFG", f"Saved configuration to {path}")

    def _on_export_csv_bundle(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export 4-Table EDMWin CSV Bundle (Select Prefix)",
            "excavation_export",
            "CSV Prefix (*)",
        )
        if path:
            bundle = self.db.export_edmwin_csv_bundle(path)
            QMessageBox.information(
                self,
                "Export CSV Bundle Complete",
                f"Exported 4-table bundle:\n"
                f"- {bundle['points']}\n"
                f"- {bundle['datums']}\n"
                f"- {bundle['prisms']}\n"
                f"- {bundle['units']}",
            )

    def _on_export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export Points CSV", "points_export.csv", "CSV (*.csv)")
        if path:
            self.db.export_points_csv(path)
            QMessageBox.information(self, "Export Complete", f"Exported points to {path}")

    def _on_import_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Points CSV", "", "CSV (*.csv)")
        if path:
            count = self.db.import_points_csv(path)
            self._refresh_all()
            QMessageBox.information(self, "Import Complete", f"Imported {count} points.")

    def _on_export_geojson(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export GeoJSON", "excavation.geojson", "GeoJSON (*.geojson *.json)")
        if path:
            self.db.export_geojson(path)
            QMessageBox.information(self, "GeoJSON Export Complete", f"Exported QGIS GeoJSON to {path}")

