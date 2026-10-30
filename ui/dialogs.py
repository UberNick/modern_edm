"""
Setup, Duplicate-ID Resolution, 6-Way Offset, Entity Management & Visual .CFG Editors for EDMWin Replacement.
Includes full parity with decompiled edmwin.exe dialogs:
- StationSetupDialog (frmStationSetup: Direct, 1-Pt, 2-Pt Backsight with hardware SetHorizontalAngle & distance residual, 3-Pt Resection, Set H-Angle & Flip 180 Degrees)
- DuplicateIDDialog (frmDupID @ 0x004399e8: Convert to alpha ID, New ID, Continuation +Shot)
- OffsetPointDialog (frmOffSet @ 0x0043946c: 6-way directional offset & +/- expression with 1km guard)
- EntityManagerDialog (frmDatumSheet, frmPolesheet with [-10,10]m height & [-0.05,0.05]m offset bounds, AddUnits)
- CFGFieldEditorDialog (frmEditField @ 0x0043eb78 / FUN_00511450: Visual .CFG field & menu editor + quick-add buttons)
- SpeedButtonEditorDialog (frmButtons @ 0x0043e310 / FUN_0051e7a0: Visual 6-button macro, Ctrl-shortcut & Alpha/Numeric ID editor)
- SerialTerminalDialog (frmTheodolite & frmDebug)
"""

from __future__ import annotations

import copy
import math
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from drivers.total_station import (
    RawObservation,
    SerialConfig,
    TotalStationDriver,
    create_driver,
    discover_bluetooth_devices,
    dms_encoded_to_radians,
    parse_transport_endpoint,
)
from storage.cfg_parser import (
    EDMConfig,
    FieldSpec,
    SpeedButtonMacro,
    build_default_paleolithic_cfg,
    save_legacy_cfg_file,
)
from storage.database import EDMDatabase
from storage.label_printer import (
    append_label_to_spool,
    format_ascii_bag_label,
    format_zpl_bag_label,
)
from survey.math_engine import (
    Datum,
    ExcavationUnit,
    Point3D,
    Prism,
    ResectionSighting,
    apply_directional_offset,
    calc_angle_dddmmss,
    flip_180_dddmmss,
    initialize_direct,
    initialize_one_point,
    initialize_resection,
    initialize_two_point_backsight,
    reduce_polar_to_cartesian,
    validate_prism_bounds,
    verify_station,
)


class DuplicateIDDialog(QDialog):
    """
    Replicates frmDupID (0x004399e8 - 'Duplicate ID. Choose which option to follow:'):
    - Convert to alpha ID (e.g. 42a)
    - New ID of <max_id + 1>
    - Continuation (+ Shot) of <current_id>
    """

    def __init__(
        self,
        unit: str,
        current_id: str,
        next_numeric_id: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Duplicate ID options")
        self.resize(420, 220)
        self.selected_option: str = "ALPHA"

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Duplicate ID ({unit} #{current_id}). Choose which option to follow:"))

        self.btn_group = QButtonGroup(self)
        self.rb_alpha = QRadioButton("Convert to alpha ID")
        self.rb_new = QRadioButton(f"New ID of {next_numeric_id}")
        self.rb_cont = QRadioButton(f"Continuation (+ Shot) of {current_id}")
        self.rb_alpha.setChecked(True)

        self.btn_group.addButton(self.rb_alpha)
        self.btn_group.addButton(self.rb_new)
        self.btn_group.addButton(self.rb_cont)

        layout.addWidget(self.rb_alpha)
        layout.addWidget(self.rb_new)
        layout.addWidget(self.rb_cont)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self._on_ok)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _on_ok(self) -> None:
        if self.rb_new.isChecked():
            self.selected_option = "NEW_ID"
        elif self.rb_cont.isChecked():
            self.selected_option = "CONTINUE"
        else:
            self.selected_option = "ALPHA"
        self.accept()


class OffsetPointDialog(QDialog):
    """
    Replicates frmOffSet (0x0043946c - 'Offset by how much:'):
    Supports 6-directional offsets (Offset East, West, North, South, Up, Down)
    with the 1-kilometer sanity check (0x004333fc).
    """

    def __init__(
        self,
        current_point: Point3D,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Offset Point Coordinates")
        self.resize(400, 210)
        self.current_point = current_point
        self.updated_point: Point3D = current_point

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.dir_combo = QComboBox()
        self.dir_combo.addItems([
            "Offset East",
            "Offset West",
            "Offset North",
            "Offset South",
            "Offset Up",
            "Offset Down",
        ])

        self.amount_input = QLineEdit("0.000")
        form.addRow("Direction:", self.dir_combo)
        form.addRow("Offset by how much (m):", self.amount_input)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("Apply Offset")
        ok_btn.clicked.connect(self._on_apply)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _on_apply(self) -> None:
        try:
            amt = float(self.amount_input.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Invalid Offset", "Offset value must be numeric")
            return
        try:
            self.updated_point = apply_directional_offset(
                self.current_point, self.dir_combo.currentText(), amt
            )
            self.accept()
        except ValueError as exc:
            QMessageBox.warning(self, "Offset Error", str(exc))


class CFGFieldEditorDialog(QDialog):
    """
    Replicates frmEditField (0x0043eb78 / FUN_00511450 - 'Add/Edit Fields'):
    - Manage up to 30 optional .CFG fields (New, Delete, Move Up/Down)
    - Configure Prompt, Maximum Length (<256), Type (Menu, Text, Numeric),
      Carry Values to New Shots, Increment, and Unit-Scoped Field
    - Manage Menu Options (Add, Edit, Delete, Move Up/Down)
    - Quick-Add system/metadata field buttons:
      * Add V-angle, H-angle and Slope-D
      * Add Date / Add Time
      * Add Datum Info (DATUMNAME, DATUMX, DATUMY, DATUMZ)
    """

    MAX_OPTIONAL_FIELDS = 30

    def __init__(
        self,
        cfg: EDMConfig,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add/Edit Fields (.CFG Schema)")
        self.resize(840, 560)
        self.cfg = cfg
        self.fields: List[FieldSpec] = copy.deepcopy(cfg.fields)
        self._current_idx: int = -1
        self._suppress_signals: bool = False

        root = QVBoxLayout(self)
        info_lbl = QLabel(
            "This form allows you to define, edit, or remove the data fields associated with the current CFG file.\n"
            "NOTE: Adding or removing fields will update the active form layout."
        )
        info_lbl.setWordWrap(True)
        root.addWidget(info_lbl)

        body = QHBoxLayout()

        # Left column: Fields list + New/Delete + Position Up/Down + Quick-Add buttons
        left_box = QGroupBox("Fields")
        left_layout = QVBoxLayout(left_box)
        self.lst_fields = QListWidget()
        self.lst_fields.currentRowChanged.connect(self._on_field_selected)
        left_layout.addWidget(self.lst_fields, 1)

        field_btn_row = QHBoxLayout()
        new_field_btn = QPushButton("New Field")
        new_field_btn.clicked.connect(self._on_new_field)
        del_field_btn = QPushButton("Delete Field")
        del_field_btn.clicked.connect(self._on_delete_field)
        up_field_btn = QPushButton("Move Up")
        up_field_btn.clicked.connect(lambda: self._move_field(-1))
        dn_field_btn = QPushButton("Move Down")
        dn_field_btn.clicked.connect(lambda: self._move_field(1))
        field_btn_row.addWidget(new_field_btn)
        field_btn_row.addWidget(del_field_btn)
        field_btn_row.addWidget(up_field_btn)
        field_btn_row.addWidget(dn_field_btn)
        left_layout.addLayout(field_btn_row)

        quick_group = QGroupBox("Quick-Add Standard EDMWin Fields")
        quick_layout = QVBoxLayout(quick_group)
        btn_angles = QPushButton("Add V-angle, H-angle and Slope-D")
        btn_angles.clicked.connect(self._on_quick_add_angles)
        btn_date = QPushButton("Add Date")
        btn_date.clicked.connect(lambda: self._on_quick_add_datetime("DATE"))
        btn_time = QPushButton("Add Time")
        btn_time.clicked.connect(lambda: self._on_quick_add_datetime("TIME"))
        btn_datum = QPushButton("Add Datum Info (DatumName, DatumX/Y/Z)")
        btn_datum.clicked.connect(self._on_quick_add_datum_info)
        quick_layout.addWidget(btn_angles)
        quick_row_dt = QHBoxLayout()
        quick_row_dt.addWidget(btn_date)
        quick_row_dt.addWidget(btn_time)
        quick_layout.addLayout(quick_row_dt)
        quick_layout.addWidget(btn_datum)
        left_layout.addWidget(quick_group)

        body.addWidget(left_box, 1)

        # Middle column: Selected Field Attributes
        mid_box = QGroupBox("Field Attributes")
        mid_form = QFormLayout(mid_box)

        self.txt_name = QLineEdit()
        self.txt_name.setReadOnly(True)
        self.txt_prompt = QLineEdit()
        self.txt_prompt.textEdited.connect(self._on_attr_edited)

        self.spn_length = QSpinBox()
        self.spn_length.setRange(1, 255)
        self.spn_length.setValue(20)
        self.spn_length.valueChanged.connect(self._on_attr_edited)

        type_row = QHBoxLayout()
        self.type_group = QButtonGroup(self)
        self.rb_menu = QRadioButton("Menu")
        self.rb_text = QRadioButton("Text")
        self.rb_numeric = QRadioButton("Numeric")
        for rb in (self.rb_menu, self.rb_text, self.rb_numeric):
            self.type_group.addButton(rb)
            type_row.addWidget(rb)
            rb.toggled.connect(self._on_attr_edited)

        self.chk_carry = QCheckBox("Carry Values to New Shots")
        self.chk_carry.toggled.connect(self._on_attr_edited)
        self.chk_increment = QCheckBox("Auto-Increment Numeric ID on New Shots")
        self.chk_increment.toggled.connect(self._on_attr_edited)
        self.chk_unit_field = QCheckBox("Unit Field (Save/Restore per Excavation Unit)")
        self.chk_unit_field.toggled.connect(self._on_attr_edited)
        self.txt_default = QLineEdit()
        self.txt_default.textEdited.connect(self._on_attr_edited)

        mid_form.addRow("Field Name:", self.txt_name)
        mid_form.addRow("Prompt:", self.txt_prompt)
        mid_form.addRow("Maximum Length (<256):", self.spn_length)
        mid_form.addRow("Type:", type_row)
        mid_form.addRow("", self.chk_carry)
        mid_form.addRow("", self.chk_increment)
        mid_form.addRow("", self.chk_unit_field)
        mid_form.addRow("Default Value:", self.txt_default)
        body.addWidget(mid_box, 1)

        # Right column: Menu Options (MenuFrame)
        self.menu_box = QGroupBox("Menu Options")
        menu_layout = QVBoxLayout(self.menu_box)
        self.lst_menu_items = QListWidget()
        menu_layout.addWidget(self.lst_menu_items, 1)

        menu_btn_row1 = QHBoxLayout()
        btn_menu_new = QPushButton("New Option")
        btn_menu_new.clicked.connect(self._on_menu_item_new)
        btn_menu_edit = QPushButton("Edit Option")
        btn_menu_edit.clicked.connect(self._on_menu_item_edit)
        btn_menu_del = QPushButton("Delete Option")
        btn_menu_del.clicked.connect(self._on_menu_item_delete)
        menu_btn_row1.addWidget(btn_menu_new)
        menu_btn_row1.addWidget(btn_menu_edit)
        menu_btn_row1.addWidget(btn_menu_del)
        menu_layout.addLayout(menu_btn_row1)

        menu_btn_row2 = QHBoxLayout()
        btn_menu_up = QPushButton("Option Up")
        btn_menu_up.clicked.connect(lambda: self._move_menu_item(-1))
        btn_menu_dn = QPushButton("Option Down")
        btn_menu_dn.clicked.connect(lambda: self._move_menu_item(1))
        menu_btn_row2.addWidget(btn_menu_up)
        menu_btn_row2.addWidget(btn_menu_dn)
        menu_layout.addLayout(menu_btn_row2)

        body.addWidget(self.menu_box, 1)
        root.addLayout(body, 1)

        # Bottom Save / Cancel bar
        footer = QHBoxLayout()
        save_btn = QPushButton("Save && Close")
        save_btn.clicked.connect(self._on_save_and_close)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        footer.addStretch()
        footer.addWidget(save_btn)
        footer.addWidget(cancel_btn)
        root.addLayout(footer)

        self._reload_field_list(0 if self.fields else -1)

    def _reload_field_list(self, select_idx: int = -1) -> None:
        self._suppress_signals = True
        try:
            self.lst_fields.clear()
            for f in self.fields:
                self.lst_fields.addItem(f.name)
        finally:
            self._suppress_signals = False

        if 0 <= select_idx < len(self.fields):
            self.lst_fields.setCurrentRow(select_idx)
        elif self.fields:
            self.lst_fields.setCurrentRow(0)
        else:
            self._current_idx = -1
            self._clear_detail_panel()

    def _clear_detail_panel(self) -> None:
        self._suppress_signals = True
        try:
            self.txt_name.clear()
            self.txt_prompt.clear()
            self.spn_length.setValue(20)
            self.rb_text.setChecked(True)
            self.chk_carry.setChecked(False)
            self.chk_increment.setChecked(False)
            self.chk_unit_field.setChecked(False)
            self.txt_default.clear()
            self.lst_menu_items.clear()
            self.menu_box.setEnabled(False)
        finally:
            self._suppress_signals = False

    def _on_field_selected(self, row: int) -> None:
        if self._suppress_signals:
            return
        self._current_idx = row
        if not (0 <= row < len(self.fields)):
            self._clear_detail_panel()
            return

        f = self.fields[row]
        self._suppress_signals = True
        try:
            self.txt_name.setText(f.name)
            self.txt_prompt.setText(f.prompt or f.name)
            self.spn_length.setValue(max(1, min(255, f.length)))
            ft = f.field_type.upper()
            if ft == "MENU":
                self.rb_menu.setChecked(True)
            elif ft in ("NUMERIC", "INTEGER", "INSTRUMENT"):
                self.rb_numeric.setChecked(True)
            else:
                self.rb_text.setChecked(True)

            self.chk_carry.setChecked(f.carry)
            self.chk_increment.setChecked(f.increment)
            self.chk_unit_field.setChecked(f.unit_field)
            self.txt_default.setText(f.default)

            self.lst_menu_items.clear()
            for item in f.menu_items:
                self.lst_menu_items.addItem(item)
            self.menu_box.setEnabled(self.rb_menu.isChecked())
        finally:
            self._suppress_signals = False

    def _on_attr_edited(self) -> None:
        if self._suppress_signals or not (0 <= self._current_idx < len(self.fields)):
            return
        f = self.fields[self._current_idx]
        f.prompt = self.txt_prompt.text().strip() or f.name
        f.length = int(self.spn_length.value())
        if self.rb_menu.isChecked():
            f.field_type = "MENU"
        elif self.rb_numeric.isChecked():
            f.field_type = "NUMERIC"
        else:
            f.field_type = "TEXT"
        f.carry = self.chk_carry.isChecked()
        f.increment = self.chk_increment.isChecked()
        f.unit_field = self.chk_unit_field.isChecked()
        f.default = self.txt_default.text().strip()
        self.menu_box.setEnabled(f.field_type == "MENU")

    def add_field_programmatically(
        self,
        name: str,
        field_type: str = "TEXT",
        prompt: str = "",
        length: int = 20,
        carry: bool = False,
        increment: bool = False,
        unit_field: bool = False,
        menu_items: Optional[List[str]] = None,
        default: str = "",
    ) -> FieldSpec:
        """
        Programmatic helper matching frmEditField validation:
        - Enforces 30 optional field limit (0x00433f8c)
        - Enforces unique field name (0x00434038)
        - Enforces length < 256 (0x00434128)
        """
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Enter New Field name")
        if len(self.fields) >= self.MAX_OPTIONAL_FIELDS:
            raise ValueError("A maximum of 30 optional fields can be defined.")
        if any(existing.name.upper() == clean_name.upper() for existing in self.fields):
            raise ValueError("Duplicate field name.")
        if not (1 <= length < 256):
            raise ValueError("Enter length less than 256")

        spec = FieldSpec(
            name=clean_name,
            field_type=field_type.upper(),
            length=length,
            prompt=prompt.strip() or clean_name,
            menu_items=list(menu_items or []),
            carry=carry,
            increment=increment,
            unit_field=unit_field,
            default=default,
        )
        self.fields.append(spec)
        self._reload_field_list(len(self.fields) - 1)
        return spec

    def _on_new_field(self) -> None:
        if len(self.fields) >= self.MAX_OPTIONAL_FIELDS:
            QMessageBox.warning(
                self, "Field Limit", "A maximum of 30 optional fields can be defined."
            )
            return
        name, ok = QInputDialog.getText(self, "Add Field", "Enter New Field name:")
        if not ok or not name.strip():
            return
        try:
            self.add_field_programmatically(
                name=name.strip(),
                field_type="TEXT",
                prompt=name.strip(),
                length=20,
                carry=False,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Add Field", str(exc))

    def _on_delete_field(self) -> None:
        if not (0 <= self._current_idx < len(self.fields)):
            return
        del self.fields[self._current_idx]
        next_idx = min(self._current_idx, len(self.fields) - 1)
        self._reload_field_list(next_idx)

    def _move_field(self, delta: int) -> None:
        if not (0 <= self._current_idx < len(self.fields)):
            return
        new_idx = self._current_idx + delta
        if not (0 <= new_idx < len(self.fields)):
            return
        self.fields[self._current_idx], self.fields[new_idx] = (
            self.fields[new_idx],
            self.fields[self._current_idx],
        )
        self._reload_field_list(new_idx)

    def _on_menu_item_new(self) -> None:
        if not (0 <= self._current_idx < len(self.fields)):
            return
        f = self.fields[self._current_idx]
        val, ok = QInputDialog.getText(
            self, "Add Value", f"Enter New Menu Item for {f.name}:"
        )
        if ok and val.strip():
            f.menu_items.append(val.strip())
            self.lst_menu_items.addItem(val.strip())
            self.lst_menu_items.setCurrentRow(self.lst_menu_items.count() - 1)

    def _on_menu_item_edit(self) -> None:
        if not (0 <= self._current_idx < len(self.fields)):
            return
        f = self.fields[self._current_idx]
        row = self.lst_menu_items.currentRow()
        if not (0 <= row < len(f.menu_items)):
            return
        val, ok = QInputDialog.getText(
            self, "Edit Menu Value", "Enter New Value:", text=f.menu_items[row]
        )
        if ok and val.strip():
            f.menu_items[row] = val.strip()
            self.lst_menu_items.item(row).setText(val.strip())

    def _on_menu_item_delete(self) -> None:
        if not (0 <= self._current_idx < len(self.fields)):
            return
        f = self.fields[self._current_idx]
        row = self.lst_menu_items.currentRow()
        if not (0 <= row < len(f.menu_items)):
            return
        del f.menu_items[row]
        self.lst_menu_items.takeItem(row)

    def _move_menu_item(self, delta: int) -> None:
        if not (0 <= self._current_idx < len(self.fields)):
            return
        f = self.fields[self._current_idx]
        row = self.lst_menu_items.currentRow()
        new_row = row + delta
        if not (0 <= row < len(f.menu_items) and 0 <= new_row < len(f.menu_items)):
            return
        f.menu_items[row], f.menu_items[new_row] = f.menu_items[new_row], f.menu_items[row]
        self._on_field_selected(self._current_idx)
        self.lst_menu_items.setCurrentRow(new_row)

    def _on_quick_add_angles(self) -> None:
        """Replicates Command1 ('Add V-angle, H-angle and Slope-D') on frmEditField."""
        for fname, prompt in [
            ("VANGLE", "V-Angle"),
            ("HANGLE", "H-Angle"),
            ("SLOPED", "Slope-D"),
        ]:
            if not any(f.name.upper() == fname for f in self.fields):
                if len(self.fields) < self.MAX_OPTIONAL_FIELDS:
                    self.fields.append(
                        FieldSpec(name=fname, field_type="NUMERIC", length=10, prompt=prompt)
                    )
        self._reload_field_list(len(self.fields) - 1)

    def _on_quick_add_datetime(self, mode: str) -> None:
        """Replicates cmdAddDateTime ('Add Date' / 'Add Time') on frmEditField."""
        fname = "DATE" if mode.upper() == "DATE" else "TIME"
        if not any(f.name.upper() == fname for f in self.fields):
            if len(self.fields) < self.MAX_OPTIONAL_FIELDS:
                self.fields.append(
                    FieldSpec(name=fname, field_type="DATETIME", length=20, prompt=fname.title())
                )
                self._reload_field_list(len(self.fields) - 1)

    def _on_quick_add_datum_info(self) -> None:
        """Replicates Command4 ('Add Datum Info') on frmEditField."""
        for fname, ftype, flen in [
            ("DATUMNAME", "TEXT", 15),
            ("DATUMX", "NUMERIC", 10),
            ("DATUMY", "NUMERIC", 10),
            ("DATUMZ", "NUMERIC", 10),
        ]:
            if not any(f.name.upper() == fname for f in self.fields):
                if len(self.fields) < self.MAX_OPTIONAL_FIELDS:
                    self.fields.append(
                        FieldSpec(name=fname, field_type=ftype, length=flen, prompt=fname.title())
                    )
        self._reload_field_list(len(self.fields) - 1)

    def _on_save_and_close(self) -> None:
        self.cfg.fields = copy.deepcopy(self.fields)
        self.cfg.unit_fields_Explicit = [
            f.name for f in self.cfg.fields if f.unit_field
        ]
        self.accept()


class SpeedButtonEditorDialog(QDialog):
    """
    Replicates frmButtons (0x0043e310 / FUN_0051e7a0 - 'Edit Buttons'):
    - 6 programmable shot buttons ([BUTTON1]..[BUTTON6]), captioned 'Setup' when undefined
    - Caption: (txtCaption)
    - Shortcut: ctrl- (txtShortcut, with conflict detection across buttons 1..6:
      'This shortcut conflicts with another one already defined.' @ 0x00435c2c)
    - ID Type: Alpha vs. Numeric (optID)
    - Add / Edit / Delete Field presets per button (AddField, EditField, DeleteField)
    - Clear Button (Command3 - returns button to undefined 'Setup' state)
    """

    NUM_BUTTONS = 6

    def __init__(
        self,
        cfg: EDMConfig,
        prisms: Optional[List[Prism]] = None,
        units: Optional[List[ExcavationUnit]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Buttons ([BUTTON1] - [BUTTON6])")
        self.resize(780, 520)
        self.cfg = cfg
        self.prisms = prisms or []
        self.units = units or []

        # Initialize 6 button slots (1..6) from cfg.button_macros
        self.slots: Dict[int, SpeedButtonMacro] = {}
        for idx in range(1, self.NUM_BUTTONS + 1):
            existing = next((m for m in cfg.button_macros if m.index == idx), None)
            if existing is not None:
                self.slots[idx] = copy.deepcopy(existing)
            else:
                self.slots[idx] = SpeedButtonMacro(
                    index=idx, title="Setup", shortcut="", id_type="NUMERIC", field_values={}
                )

        self._active_slot: int = 1
        self._suppress_signals: bool = False

        root = QVBoxLayout(self)
        help_lbl = QLabel(
            "Buttons allow you to predefine shot types that will automatically fill in values for fields.\n"
            "To define a button, click one of the 6 slots below, set its Caption, optional Ctrl-Shortcut, "
            "and add field/value pairs."
        )
        help_lbl.setWordWrap(True)
        root.addWidget(help_lbl)

        # Top row: 6 programmable buttons (Button 1 .. Button 6)
        slot_box = QGroupBox("Select Shot Button Slot (1 - 6)")
        slot_row = QHBoxLayout(slot_box)
        self.slot_buttons: Dict[int, QPushButton] = {}
        for idx in range(1, self.NUM_BUTTONS + 1):
            b = QPushButton()
            b.setCheckable(True)
            b.clicked.connect(lambda _, i=idx: self.select_slot(i))
            self.slot_buttons[idx] = b
            slot_row.addWidget(b)
        root.addWidget(slot_box)

        # Middle split: Left = Button Properties & Add Field, Right = Assigned Field Values Table
        mid_layout = QHBoxLayout()

        prop_box = QGroupBox("Current Button Settings")
        prop_form = QFormLayout(prop_box)

        self.lbl_current_button = QLabel("Button 1")
        self.txt_caption = QLineEdit()
        self.txt_caption.setPlaceholderText("Setup (or e.g. LITHIC, BONE)")
        self.txt_caption.textEdited.connect(self._on_caption_edited)

        self.txt_shortcut = QLineEdit()
        self.txt_shortcut.setMaxLength(1)
        self.txt_shortcut.setPlaceholderText("Single key (e.g. 1, B)")
        self.txt_shortcut.textEdited.connect(self._on_shortcut_edited)

        id_type_row = QHBoxLayout()
        self.id_group = QButtonGroup(self)
        self.rb_id_numeric = QRadioButton("Numeric")
        self.rb_id_alpha = QRadioButton("Alpha")
        self.id_group.addButton(self.rb_id_numeric)
        self.id_group.addButton(self.rb_id_alpha)
        id_type_row.addWidget(self.rb_id_numeric)
        id_type_row.addWidget(self.rb_id_alpha)
        self.rb_id_numeric.toggled.connect(self._on_id_type_toggled)

        self.cbo_add_field = QComboBox()
        self._populate_available_fields()
        self.cbo_add_field.currentTextChanged.connect(self._on_field_choice_changed)

        self.cbo_field_value = QComboBox()
        self.cbo_field_value.setEditable(True)

        btn_set_field = QPushButton("Add / Update Field Value")
        btn_set_field.clicked.connect(self._on_set_field_value)
        btn_del_field = QPushButton("Delete Selected Field")
        btn_del_field.clicked.connect(self._on_delete_field_value)
        btn_clear_slot = QPushButton("Clear Button (Reset to 'Setup')")
        btn_clear_slot.clicked.connect(self._on_clear_slot)

        prop_form.addRow("Current Button:", self.lbl_current_button)
        prop_form.addRow("Caption:", self.txt_caption)
        prop_form.addRow("Shortcut: ctrl-", self.txt_shortcut)
        prop_form.addRow("ID Type:", id_type_row)
        prop_form.addRow("Field:", self.cbo_add_field)
        prop_form.addRow("Value:", self.cbo_field_value)
        prop_form.addRow(btn_set_field)
        prop_form.addRow(btn_del_field)
        prop_form.addRow(btn_clear_slot)
        mid_layout.addWidget(prop_box, 1)

        table_box = QGroupBox("Assigned Fields & Values for Current Button")
        table_layout = QVBoxLayout(table_box)
        self.tbl_assignments = QTableWidget()
        self.tbl_assignments.setColumnCount(2)
        self.tbl_assignments.setHorizontalHeaderLabels(["Field Name", "Preset Value"])
        self.tbl_assignments.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_assignments.cellClicked.connect(self._on_assignment_clicked)
        table_layout.addWidget(self.tbl_assignments, 1)
        mid_layout.addWidget(table_box, 1)

        root.addLayout(mid_layout, 1)

        # Bottom Close (Save) / Cancel
        footer = QHBoxLayout()
        close_save_btn = QPushButton("Close (Save Changes)")
        close_save_btn.clicked.connect(self._on_save_and_close)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        footer.addStretch()
        footer.addWidget(close_save_btn)
        footer.addWidget(cancel_btn)
        root.addLayout(footer)

        self._refresh_slot_captions()
        self.select_slot(1)

    def _populate_available_fields(self) -> None:
        self.cbo_add_field.clear()
        seen: List[str] = []
        for f in self.cfg.fields:
            if f.name.upper() not in ("ID", "SUFFIX") and f.name not in seen:
                seen.append(f.name)
        for sys_f in ("PRISM", "UNIT"):
            if all(s.upper() != sys_f for s in seen):
                seen.append(sys_f)
        self.cbo_add_field.addItems(seen)

    def _on_field_choice_changed(self, field_name: str) -> None:
        self.cbo_field_value.clear()
        fn_upper = field_name.strip().upper()
        if fn_upper == "PRISM":
            self.cbo_field_value.addItems([p.name for p in self.prisms])
            return
        if fn_upper == "UNIT":
            unit_items = [u.name for u in self.units]
            for f in self.cfg.fields:
                if f.name.upper() == "UNIT":
                    for m in f.menu_items:
                        if m not in unit_items:
                            unit_items.append(m)
            self.cbo_field_value.addItems(unit_items)
            return
        for f in self.cfg.fields:
            if f.name.upper() == fn_upper:
                if f.menu_items:
                    self.cbo_field_value.addItems(f.menu_items)
                elif f.default:
                    self.cbo_field_value.setEditText(f.default)
                return

    def _refresh_slot_captions(self) -> None:
        for idx, btn in self.slot_buttons.items():
            macro = self.slots[idx]
            title = macro.title.strip() if macro.title.strip() else "Setup"
            sc = f" [^{macro.shortcut}]" if macro.shortcut else ""
            btn.setText(f"{idx}: {title}{sc}")
            btn.setChecked(idx == self._active_slot)

    def select_slot(self, slot_idx: int) -> None:
        if not (1 <= slot_idx <= self.NUM_BUTTONS):
            return
        self._active_slot = slot_idx
        macro = self.slots[slot_idx]
        self._suppress_signals = True
        try:
            self.lbl_current_button.setText(f"Button {slot_idx}")
            self.txt_caption.setText("" if macro.title == "Setup" else macro.title)
            self.txt_shortcut.setText(macro.shortcut)
            if macro.id_type.upper() == "ALPHA":
                self.rb_id_alpha.setChecked(True)
            else:
                self.rb_id_numeric.setChecked(True)
            self._reload_assignments_table()
            self._refresh_slot_captions()
        finally:
            self._suppress_signals = False

    def _reload_assignments_table(self) -> None:
        macro = self.slots[self._active_slot]
        items = list(macro.field_values.items())
        self.tbl_assignments.setRowCount(len(items))
        for r, (k, v) in enumerate(items):
            self.tbl_assignments.setItem(r, 0, QTableWidgetItem(str(k)))
            self.tbl_assignments.setItem(r, 1, QTableWidgetItem(str(v)))

    def _on_assignment_clicked(self, row: int, _col: int) -> None:
        k_item = self.tbl_assignments.item(row, 0)
        v_item = self.tbl_assignments.item(row, 1)
        if k_item and v_item:
            self.cbo_add_field.setCurrentText(k_item.text())
            self.cbo_field_value.setCurrentText(v_item.text())

    def _on_caption_edited(self, text: str) -> None:
        if self._suppress_signals:
            return
        self.slots[self._active_slot].title = text.strip() or "Setup"
        self._refresh_slot_captions()

    def set_slot_shortcut(self, slot_idx: int, shortcut_char: str) -> None:
        """
        Programmatic helper matching frmButtons shortcut conflict check (0x00435c2c):
        Raises ValueError('This shortcut conflicts with another one already defined.')
        if another button slot already uses the same shortcut key.
        """
        sc = shortcut_char.strip()
        if sc:
            for other_idx, other_macro in self.slots.items():
                if other_idx != slot_idx and other_macro.shortcut.upper() == sc.upper():
                    raise ValueError("This shortcut conflicts with another one already defined.")
        self.slots[slot_idx].shortcut = sc
        self._refresh_slot_captions()

    def _on_shortcut_edited(self, text: str) -> None:
        if self._suppress_signals:
            return
        try:
            self.set_slot_shortcut(self._active_slot, text)
        except ValueError as exc:
            QMessageBox.warning(self, "Shortcut Conflict", str(exc))
            self._suppress_signals = True
            try:
                self.txt_shortcut.setText("")
                self.slots[self._active_slot].shortcut = ""
                self._refresh_slot_captions()
            finally:
                self._suppress_signals = False

    def _on_id_type_toggled(self) -> None:
        if self._suppress_signals:
            return
        self.slots[self._active_slot].id_type = (
            "ALPHA" if self.rb_id_alpha.isChecked() else "NUMERIC"
        )

    def set_slot_field_value(self, slot_idx: int, field_name: str, value: str) -> None:
        """Sets or updates a field preset on the given button slot."""
        fn = field_name.strip()
        val = value.strip()
        if not fn:
            return
        macro = self.slots[slot_idx]
        macro.field_values[fn] = val
        if macro.title == "Setup" and val:
            macro.title = val
        if slot_idx == self._active_slot:
            self._reload_assignments_table()
            self.txt_caption.setText("" if macro.title == "Setup" else macro.title)
        self._refresh_slot_captions()

    def _on_set_field_value(self) -> None:
        fn = self.cbo_add_field.currentText().strip()
        val = self.cbo_field_value.currentText().strip()
        if not fn:
            return
        self.set_slot_field_value(self._active_slot, fn, val)

    def _on_delete_field_value(self) -> None:
        macro = self.slots[self._active_slot]
        row = self.tbl_assignments.currentRow()
        if row >= 0:
            k_item = self.tbl_assignments.item(row, 0)
            if k_item and k_item.text() in macro.field_values:
                del macro.field_values[k_item.text()]
                self._reload_assignments_table()
                return
        fn = self.cbo_add_field.currentText().strip()
        if fn in macro.field_values:
            del macro.field_values[fn]
            self._reload_assignments_table()

    def clear_slot(self, slot_idx: int) -> None:
        """Replicates Command3 ('Clear Button') on frmButtons."""
        self.slots[slot_idx] = SpeedButtonMacro(
            index=slot_idx, title="Setup", shortcut="", id_type="NUMERIC", field_values={}
        )
        if slot_idx == self._active_slot:
            self.select_slot(slot_idx)
        else:
            self._refresh_slot_captions()

    def _on_clear_slot(self) -> None:
        self.clear_slot(self._active_slot)

    def _on_save_and_close(self) -> None:
        defined_macros: List[SpeedButtonMacro] = []
        speed_titles: List[str] = []
        for idx in range(1, self.NUM_BUTTONS + 1):
            m = self.slots[idx]
            if m.title.strip() and m.title.strip().upper() != "SETUP" and m.field_values:
                defined_macros.append(copy.deepcopy(m))
                speed_titles.append(m.title.strip())
        self.cfg.button_macros = defined_macros
        self.cfg.speed_buttons = speed_titles
        self.accept()


class SerialTerminalDialog(QDialog):
    """
    Replicates frmTheodolite (0x00438e68) & Connection Manager with full support for:
    - RS-232 / USB-Serial COM ports (with Baud, Parity, Data Bits, Stop Bits, DTR/RTS, Flow Control, EdmDelayTime, Topcon BCC & Barrel Reversal checks)
    - Bluetooth Classic SPP Virtual COM & Direct RFCOMM Sockets ('bt://AA:BB:CC:DD:EE:FF:1') + Paired Bluetooth Scanner
    - Bluetooth Low Energy (BLE) UART ('ble://AA:BB:CC:DD:EE:FF')
    - Wi-Fi / Ethernet TCP/IP ('tcp://192.168.1.100:5000') & UDP ('udp://127.0.0.1:5005') robotic bridges
    - Multi-Protocol Virtual Hardware Emulators ('SIMULATOR', 'VIRT-LEICA', 'VIRT-TOPCON', 'VIRT-SOKKIA', 'VIRT-TRIMBLE', 'BT-SIM-LEICA', 'TCP-SIM-STATION')
    """

    def __init__(
        self,
        station_type: str,
        serial_config: SerialConfig,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Total Station Connection Manager (Serial / Bluetooth / TCP-IP / Emulator)")
        self.resize(740, 560)
        self.station_type = station_type
        self.serial_config = serial_config

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.station_combo = QComboBox()
        self.station_combo.addItems([
            "Simulator",
            "LeicaGeoCOM",
            "Leica_AutoTilt",
            "LeicaGSI",
            "Builder",
            "Topcon",
            "Sokkia",
            "Nikon",
            "Trimble",
            "Zeiss",
            "Microscribe",
        ])
        self.station_combo.setCurrentText(self.station_type)

        self.transport_combo = QComboBox()
        self.transport_combo.addItems([
            "Serial / USB / Bluetooth SPP COM Port",
            "Bluetooth RFCOMM Direct (bt://MAC:Channel)",
            "Bluetooth Low Energy UART (ble://MAC)",
            "Wi-Fi / Ethernet TCP/IP Socket (tcp://host:port)",
            "UDP Stream Bridge (udp://host:port)",
            "Virtual Hardware Emulator (SIMULATOR / VIRT-*)",
        ])
        self.transport_combo.currentIndexChanged.connect(self._on_transport_mode_changed)

        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self._refresh_ports(include_bt=False)
        self.port_combo.setCurrentText(self.serial_config.port)

        scan_btn = QPushButton("Scan COM Ports")
        scan_btn.clicked.connect(lambda: self._refresh_ports(include_bt=False))
        scan_bt_btn = QPushButton("Scan Bluetooth Devices")
        scan_bt_btn.clicked.connect(self._on_scan_bluetooth)
        port_row = QHBoxLayout()
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(scan_btn)
        port_row.addWidget(scan_bt_btn)

        # Serial framing row: Baud, Parity, Data Bits, Stop Bits, Flow Control
        serial_row = QHBoxLayout()
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"])
        self.baud_combo.setCurrentText(str(self.serial_config.baudrate))

        self.parity_combo = QComboBox()
        self.parity_combo.addItems(["N", "E", "O"])
        self.parity_combo.setCurrentText(self.serial_config.parity)

        self.bytesize_combo = QComboBox()
        self.bytesize_combo.addItems(["7", "8"])
        self.bytesize_combo.setCurrentText(str(self.serial_config.bytesize))

        self.stopbits_combo = QComboBox()
        self.stopbits_combo.addItems(["1", "2"])
        self.stopbits_combo.setCurrentText(str(self.serial_config.stopbits))

        self.flow_combo = QComboBox()
        self.flow_combo.addItems(["NONE", "RTSCTS", "XONXOFF"])
        self.flow_combo.setCurrentText(getattr(self.serial_config, "flow_control", "NONE"))

        serial_row.addWidget(QLabel("Baud:"))
        serial_row.addWidget(self.baud_combo)
        serial_row.addWidget(QLabel("Parity:"))
        serial_row.addWidget(self.parity_combo)
        serial_row.addWidget(QLabel("Bits:"))
        serial_row.addWidget(self.bytesize_combo)
        serial_row.addWidget(QLabel("Stop:"))
        serial_row.addWidget(self.stopbits_combo)
        serial_row.addWidget(QLabel("Flow:"))
        serial_row.addWidget(self.flow_combo)

        # Hardware flags & EdmDelayTime row (matches frmTheodolite)
        flags_row = QHBoxLayout()
        self.chk_dtr = QCheckBox("DTR")
        self.chk_dtr.setChecked(self.serial_config.dtr)
        self.chk_rts = QCheckBox("RTS")
        self.chk_rts.setChecked(self.serial_config.rts)
        self.chk_bcc = QCheckBox("Verify Topcon BCC")
        self.chk_bcc.setChecked(self.serial_config.verify_bcc)
        self.chk_barrel = QCheckBox("Check Barrel Reversal")
        self.chk_barrel.setChecked(self.serial_config.check_barrel_reversal)
        self.spn_delay = QDoubleSpinBox()
        self.spn_delay.setRange(0.0, 10.0)
        self.spn_delay.setSingleStep(0.1)
        self.spn_delay.setDecimals(2)
        self.spn_delay.setValue(self.serial_config.edm_delay_time)

        flags_row.addWidget(self.chk_dtr)
        flags_row.addWidget(self.chk_rts)
        flags_row.addWidget(self.chk_bcc)
        flags_row.addWidget(self.chk_barrel)
        flags_row.addWidget(QLabel("EDM Delay (s):"))
        flags_row.addWidget(self.spn_delay)
        flags_row.addStretch()

        form.addRow("Station Protocol:", self.station_combo)
        form.addRow("Connection Transport:", self.transport_combo)
        form.addRow("Port / BT MAC / TCP URI:", port_row)
        form.addRow("Serial Framing:", serial_row)
        form.addRow("Protocol Options:", flags_row)
        layout.addLayout(form)

        cmd_row = QHBoxLayout()
        self.raw_cmd_input = QLineEdit("%R1Q,2108:5000,1")
        send_raw_btn = QPushButton("Send Raw ASCII")
        send_raw_btn.clicked.connect(self._on_send_raw)
        test_meas_btn = QPushButton("Test Measure Command")
        test_meas_btn.clicked.connect(self._on_test_measure)
        cmd_row.addWidget(self.raw_cmd_input, 1)
        cmd_row.addWidget(send_raw_btn)
        cmd_row.addWidget(test_meas_btn)
        layout.addLayout(cmd_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 1)

        btn_row = QHBoxLayout()
        apply_btn = QPushButton("Save & Apply Settings")
        apply_btn.clicked.connect(self.accept)
        close_btn = QPushButton("Cancel")
        close_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(apply_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _on_transport_mode_changed(self, idx: int) -> None:
        presets = {
            1: "bt://00:11:22:33:44:55:1",
            2: "ble://00:11:22:33:44:55",
            3: "tcp://192.168.1.100:5000",
            4: "udp://127.0.0.1:5005",
            5: "SIMULATOR",
        }
        if idx in presets:
            self.port_combo.setEditText(presets[idx])

    def _refresh_ports(self, include_bt: bool = False) -> None:
        self.port_combo.clear()
        for dev, desc in TotalStationDriver.list_available_ports(include_bluetooth_scan=include_bt):
            self.port_combo.addItem(desc, dev)

    def _on_scan_bluetooth(self) -> None:
        bt_devs = discover_bluetooth_devices()
        self._refresh_ports(include_bt=True)
        self._append_log(
            "BLUETOOTH",
            f"Discovered {len(bt_devs)} Bluetooth SPP / RFCOMM / Simulated endpoint(s): "
            + ", ".join(d[0] for d in bt_devs),
        )
        if bt_devs:
            self.port_combo.setCurrentText(bt_devs[0][1])

    def _append_log(self, direction: str, text: str) -> None:
        self.log_view.appendPlainText(f"[{direction}] {text}")

    def build_config(self) -> Tuple[str, SerialConfig]:
        txt = self.port_combo.currentText().strip()
        port_str = txt.split(" - ")[0].strip() if " - " in txt else txt
        if not port_str:
            port_data = self.port_combo.currentData()
            port_str = str(port_data) if port_data else "SIMULATOR"
        cfg = SerialConfig(
            port=port_str,
            baudrate=int(self.baud_combo.currentText() or "19200"),
            bytesize=int(self.bytesize_combo.currentText() or "8"),
            parity=self.parity_combo.currentText() or "N",
            stopbits=int(self.stopbits_combo.currentText() or "1"),
            edm_delay_time=float(self.spn_delay.value()),
            dtr=self.chk_dtr.isChecked(),
            rts=self.chk_rts.isChecked(),
            flow_control=self.flow_combo.currentText() or "NONE",
            verify_bcc=self.chk_bcc.isChecked(),
            check_barrel_reversal=self.chk_barrel.isChecked(),
        )
        return self.station_combo.currentText(), cfg

    def _on_send_raw(self) -> None:
        st_type, cfg = self.build_config()
        drv = create_driver(st_type, cfg, self._append_log)
        try:
            cmd = self.raw_cmd_input.text() + "\r\n"
            drv.send_raw(cmd)
        except Exception as exc:
            self._append_log("ERROR", str(exc))
        finally:
            drv.disconnect()

    def _on_test_measure(self) -> None:
        st_type, cfg = self.build_config()
        drv = create_driver(st_type, cfg, self._append_log)
        try:
            obs = drv.measure()
            self._append_log(
                "PARSED",
                f"SD={obs.slope_distance:.4f}m | Hz={obs.horizontal_angle_deg:.4f}° | V={obs.vertical_angle_deg:.4f}°",
            )
        except Exception as exc:
            self._append_log("ERROR", str(exc))
        finally:
            drv.disconnect()


class StationSetupDialog(QDialog):
    def __init__(
        self,
        db: EDMDatabase,
        driver: TotalStationDriver,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Initialize / Verify Total Station")
        self.resize(740, 540)
        self.db = db
        self.driver = driver
        self.datums = self.db.list_datums()
        self.prisms = self.db.list_prisms()
        self.resection_sightings: List[ResectionSighting] = []

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_direct_tab(), "1. Direct / Set Angle")
        self.tabs.addTab(self._build_one_pt_tab(), "2. 1-Point")
        self.tabs.addTab(self._build_two_pt_tab(), "3. 2-Pt Backsight")
        self.tabs.addTab(self._build_resection_tab(), "4. Resection (2+ Pts)")
        self.tabs.addTab(self._build_verify_tab(), "5. Verify Station")
        layout.addWidget(self.tabs, 1)

        self.status_box = QPlainTextEdit()
        self.status_box.setReadOnly(True)
        self.status_box.setMaximumHeight(140)
        layout.addWidget(self.status_box)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

    def _datum_combo(self) -> QComboBox:
        cb = QComboBox()
        for d in self.datums:
            cb.addItem(f"{d.name} ({d.x:.3f}, {d.y:.3f}, {d.z:.3f})", d)
        return cb

    def _prism_combo(self) -> QComboBox:
        cb = QComboBox()
        for p in self.prisms:
            cb.addItem(f"{p.name} (HT={p.height:.3f}m, Off={p.offset:.3f}m)", p)
        return cb

    def _hi_spin(self) -> QDoubleSpinBox:
        sp = QDoubleSpinBox()
        sp.setRange(-10.0, 50.0)
        sp.setDecimals(4)
        sp.setValue(self.db.load_station_state().instrument_height)
        return sp

    def _build_direct_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.dir_datum_cb = self._datum_combo()
        self.dir_hi_sp = self._hi_spin()
        self.dir_hangle_edit = QLineEdit("0.0000")

        angle_btn_row = QHBoxLayout()
        set_hangle_btn = QPushButton("Set Horizontal Angle")
        set_hangle_btn.clicked.connect(self._on_set_hangle_direct)
        flip_btn = QPushButton("Flip 180 Degrees")
        flip_btn.clicked.connect(self._on_flip_180)
        angle_btn_row.addWidget(self.dir_hangle_edit, 1)
        angle_btn_row.addWidget(set_hangle_btn)
        angle_btn_row.addWidget(flip_btn)

        btn = QPushButton("Initialize Direct Over Datum")
        btn.clicked.connect(self._run_direct)
        form.addRow("Occupied Datum:", self.dir_datum_cb)
        form.addRow("Instrument Height (m):", self.dir_hi_sp)
        form.addRow("Horizontal Angle (DDD.MMSS):", angle_btn_row)
        form.addRow(btn)
        return w

    def _on_flip_180(self) -> None:
        flipped = flip_180_dddmmss(self.dir_hangle_edit.text())
        self.dir_hangle_edit.setText(flipped)

    def _on_set_hangle_direct(self) -> None:
        try:
            angle_val = float(self.dir_hangle_edit.text().strip() or "0.0")
            self.driver.set_horizontal_angle(angle_val)
            self.status_box.setPlainText(
                f"Angle set to {angle_val:.4f} (DDD.MMSS). Verify against total station display."
            )
        except Exception as exc:
            QMessageBox.warning(self, "Set Horizontal Angle Error", str(exc))

    def _build_one_pt_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.one_datum_cb = self._datum_combo()
        self.one_prism_cb = self._prism_combo()
        self.one_hi_sp = self._hi_spin()
        btn = QPushButton("Shoot Datum & Initialize 1-Point Station")
        btn.clicked.connect(self._run_one_pt)
        form.addRow("Sighted Datum:", self.one_datum_cb)
        form.addRow("Prism:", self.one_prism_cb)
        form.addRow("Instrument Height (m):", self.one_hi_sp)
        form.addRow(btn)
        return w

    def _build_two_pt_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.two_occ_cb = self._datum_combo()
        self.two_bs_cb = self._datum_combo()
        if self.two_bs_cb.count() > 1:
            self.two_bs_cb.setCurrentIndex(1)
        self.two_prism_cb = self._prism_combo()
        self.two_hi_sp = self._hi_spin()
        btn = QPushButton("Shoot Backsight & Initialize 2-Point Station")
        btn.clicked.connect(self._run_two_pt)
        form.addRow("Occupied Datum:", self.two_occ_cb)
        form.addRow("Backsight Datum:", self.two_bs_cb)
        form.addRow("Backsight Prism:", self.two_prism_cb)
        form.addRow("Instrument Height (m):", self.two_hi_sp)
        form.addRow(btn)
        return w

    def _build_resection_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        form = QFormLayout()
        self.res_datum_cb = self._datum_combo()
        self.res_prism_cb = self._prism_combo()
        self.res_hi_sp = self._hi_spin()
        form.addRow("Target Datum:", self.res_datum_cb)
        form.addRow("Target Prism:", self.res_prism_cb)
        form.addRow("Instrument Height (m):", self.res_hi_sp)
        v.addLayout(form)

        row = QHBoxLayout()
        add_shot_btn = QPushButton("Measure & Add Datum Sighting")
        add_shot_btn.clicked.connect(self._add_resection_shot)
        solve_btn = QPushButton("Solve Resection & Save Station")
        solve_btn.clicked.connect(self._solve_resection)
        clear_btn = QPushButton("Clear Sightings")
        clear_btn.clicked.connect(self._clear_resection)
        row.addWidget(add_shot_btn)
        row.addWidget(solve_btn)
        row.addWidget(clear_btn)
        v.addLayout(row)
        return w

    def _build_verify_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.ver_datum_cb = self._datum_combo()
        self.ver_prism_cb = self._prism_combo()
        btn = QPushButton("Shoot Datum & Compute Residuals (dX, dY, dZ)")
        btn.clicked.connect(self._run_verify)
        form.addRow("Check Datum:", self.ver_datum_cb)
        form.addRow("Prism:", self.ver_prism_cb)
        form.addRow(btn)
        return w

    def _run_direct(self) -> None:
        datum: Datum = self.dir_datum_cb.currentData()
        try:
            h_angle_val = float(self.dir_hangle_edit.text().strip() or "0.0")
        except ValueError:
            h_angle_val = 0.0
        stn = initialize_direct(
            occupied_datum=datum,
            instrument_height=self.dir_hi_sp.value(),
            azimuth_offset_rad=0.0,
        )
        self.db.save_station_state(stn)
        self.status_box.setPlainText(
            f"[DIRECT OK] Station={stn.station_name} | X={stn.x:.4f}, Y={stn.y:.4f}, "
            f"Ground Z={stn.z:.4f}, Center Z={stn.instrument_center_z:.4f} (HAngle={h_angle_val:.4f})"
        )

    def _run_one_pt(self) -> None:
        try:
            datum: Datum = self.one_datum_cb.currentData()
            prism: Prism = self.one_prism_cb.currentData()
            obs = self.driver.measure()
            stn = initialize_one_point(
                target_datum=datum,
                instrument_height=self.one_hi_sp.value(),
                prism_height=prism.height,
                slope_distance=obs.slope_distance,
                horizontal_angle_rad=obs.horizontal_angle_rad,
                vertical_angle_rad=obs.vertical_angle_rad,
                prism_offset=prism.offset,
            )
            self.db.save_station_state(stn)
            self.status_box.setPlainText(
                f"[1-POINT OK] Station solved at X={stn.x:.4f}, Y={stn.y:.4f}, "
                f"Ground Z={stn.z:.4f}, Center Z={stn.instrument_center_z:.4f}"
            )
        except Exception as exc:
            QMessageBox.critical(self, "1-Point Setup Error", str(exc))

    def _run_two_pt(self) -> None:
        try:
            occ: Datum = self.two_occ_cb.currentData()
            bs: Datum = self.two_bs_cb.currentData()
            prism: Prism = self.two_prism_cb.currentData()

            _, _, _, dms_str = calc_angle_dddmmss(occ.x, occ.y, bs.x, bs.y)
            self.driver.set_horizontal_angle(float(dms_str))

            obs = self.driver.measure()
            stn, res = initialize_two_point_backsight(
                occupied_datum=occ,
                backsight_datum=bs,
                instrument_height=self.two_hi_sp.value(),
                prism_height=prism.height,
                slope_distance=obs.slope_distance,
                horizontal_angle_rad=obs.horizontal_angle_rad,
                vertical_angle_rad=obs.vertical_angle_rad,
                prism_offset=prism.offset,
            )
            self.db.save_station_state(stn)
            self.status_box.setPlainText(
                f"[2-PT BACKSIGHT OK] Hardware H-Angle set to {dms_str} | Azimuth Offset={math.degrees(stn.azimuth_offset_rad):.4f}°\n"
                f"Error in measured distance between first and second datums is {res.distance_difference:+.4f}m "
                f"(Expected={res.expected_distance:.3f}m, Recorded={res.recorded_distance:.3f}m)\n"
                f"Residuals on {bs.name}: dX={res.dx:+.4f}m, dY={res.dy:+.4f}m, dZ={res.dz:+.4f}m (2D Err={res.error_2d:.4f}m)"
            )
        except Exception as exc:
            QMessageBox.critical(self, "2-Point Backsight Error", str(exc))

    def _add_resection_shot(self) -> None:
        try:
            datum: Datum = self.res_datum_cb.currentData()
            prism: Prism = self.res_prism_cb.currentData()
            obs = self.driver.measure()
            self.resection_sightings.append(
                ResectionSighting(
                    datum=datum,
                    slope_distance=obs.slope_distance,
                    horizontal_angle_rad=obs.horizontal_angle_rad,
                    vertical_angle_rad=obs.vertical_angle_rad,
                    prism_height=prism.height,
                    prism_offset=prism.offset,
                )
            )
            self.status_box.appendPlainText(
                f"Added sighting #{len(self.resection_sightings)} -> {datum.name}: "
                f"SD={obs.slope_distance:.4f}m, Hz={obs.horizontal_angle_deg:.4f}°, V={obs.vertical_angle_deg:.4f}°"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Measurement Error", str(exc))

    def _solve_resection(self) -> None:
        try:
            res = initialize_resection(
                sightings=self.resection_sightings,
                instrument_height=self.res_hi_sp.value(),
            )
            self.db.save_station_state(res.station)
            lines = [
                f"[RESECTION SOLVED] X={res.station.x:.4f}, Y={res.station.y:.4f}, "
                f"Ground Z={res.station.z:.4f}, Center Z={res.station.instrument_center_z:.4f} | "
                f"RMS 2D={res.rms_2d:.4f}m, RMS Z={res.rms_z:.4f}m"
            ]
            for r in res.residuals:
                lines.append(f"  {r.datum_name}: dX={r.dx:+.4f}m, dY={r.dy:+.4f}m, dZ={r.dz:+.4f}m")
            self.status_box.setPlainText("\n".join(lines))
        except Exception as exc:
            QMessageBox.critical(self, "Resection Error", str(exc))

    def _clear_resection(self) -> None:
        self.resection_sightings.clear()
        self.status_box.setPlainText("Cleared resection sightings.")

    def _run_verify(self) -> None:
        try:
            datum: Datum = self.ver_datum_cb.currentData()
            prism: Prism = self.ver_prism_cb.currentData()
            stn = self.db.load_station_state()
            obs = self.driver.measure()
            res = verify_station(
                station=stn,
                known_datum=datum,
                slope_distance=obs.slope_distance,
                horizontal_angle_rad=obs.horizontal_angle_rad,
                vertical_angle_rad=obs.vertical_angle_rad,
                prism_height=prism.height,
                prism_offset=prism.offset,
            )
            self.status_box.setPlainText(
                f"[VERIFY -> {datum.name}] dX={res.dx:+.4f}m | dY={res.dy:+.4f}m | dZ={res.dz:+.4f}m\n"
                f"Expected Dist={res.expected_distance:.3f}m | Recorded={res.recorded_distance:.3f}m | Diff={res.distance_difference:+.3f}m\n"
                f"2D Error = {res.error_2d:.4f}m | 3D Error = {res.error_3d:.4f}m"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Verify Station Error", str(exc))


class EntityManagerDialog(QDialog):
    def __init__(self, db: EDMDatabase, mode: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.db = db
        self.mode = mode.upper()
        self.setWindowTitle(f"Manage {self.mode.title()}")
        self.resize(650, 420)

        layout = QVBoxLayout(self)
        self.table = QTableWidget()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        form_row = QHBoxLayout()
        self.inputs: List[QLineEdit] = []
        placeholders = self._get_placeholders()
        for ph in placeholders:
            le = QLineEdit()
            le.setPlaceholderText(ph)
            form_row.addWidget(le)
            self.inputs.append(le)

        add_btn = QPushButton("Add / Update")
        add_btn.clicked.connect(self._on_upsert)
        del_btn = QPushButton("Delete Selected")
        del_btn.clicked.connect(self._on_delete)
        form_row.addWidget(add_btn)
        form_row.addWidget(del_btn)
        layout.addLayout(form_row)

        self._reload_table()

    def _get_placeholders(self) -> List[str]:
        if self.mode == "DATUMS":
            return ["Name", "X", "Y", "Z", "Description"]
        if self.mode == "PRISMS":
            return ["Name (<=15 chars)", "Height [-10..10m]", "Offset [-0.05..0.05m]", "Description"]
        return ["Unit Name", "Shape (RECT/CIRCLE/NOLIMITS)", "MinX / CenterX", "MinY / CenterY", "MaxX / Radius", "MaxY"]

    def _reload_table(self) -> None:
        if self.mode == "DATUMS":
            items = self.db.list_datums()
            self.table.setColumnCount(5)
            self.table.setHorizontalHeaderLabels(["Name", "X", "Y", "Z", "Description"])
            self.table.setRowCount(len(items))
            for r, d in enumerate(items):
                for c, val in enumerate([d.name, f"{d.x:.4f}", f"{d.y:.4f}", f"{d.z:.4f}", d.description]):
                    self.table.setItem(r, c, QTableWidgetItem(str(val)))
        elif self.mode == "PRISMS":
            items = self.db.list_prisms()
            self.table.setColumnCount(4)
            self.table.setHorizontalHeaderLabels(["Name", "Height (m)", "Offset (m)", "Description"])
            self.table.setRowCount(len(items))
            for r, p in enumerate(items):
                for c, val in enumerate([p.name, f"{p.height:.3f}", f"{p.offset:.3f}", p.description]):
                    self.table.setItem(r, c, QTableWidgetItem(str(val)))
        else:
            items = self.db.list_units()
            self.table.setColumnCount(6)
            self.table.setHorizontalHeaderLabels(["Name", "Shape", "MinX/CtrX", "MinY/CtrY", "MaxX/Rad", "MaxY"])
            self.table.setRowCount(len(items))
            for r, u in enumerate(items):
                vals = (
                    [u.name, u.shape, f"{u.center_x:.3f}", f"{u.center_y:.3f}", f"{u.radius:.3f}", "-"]
                    if u.shape.upper() == "CIRCLE"
                    else [u.name, u.shape, f"{u.min_x:.3f}", f"{u.min_y:.3f}", f"{u.max_x:.3f}", f"{u.max_y:.3f}"]
                )
                for c, val in enumerate(vals):
                    self.table.setItem(r, c, QTableWidgetItem(str(val)))

    def _on_upsert(self) -> None:
        try:
            vals = [inp.text().strip() for inp in self.inputs]
            if not vals[0]:
                return
            if self.mode == "DATUMS":
                self.db.upsert_datum(Datum(vals[0], float(vals[1]), float(vals[2]), float(vals[3]), vals[4]))
            elif self.mode == "PRISMS":
                if len(vals[0]) > 15:
                    raise ValueError("Enter Name for prism (15 chars or less)")
                h_val = float(vals[1])
                off_val = float(vals[2] or 0.0)
                validate_prism_bounds(h_val, off_val)
                self.db.upsert_prism(Prism(vals[0], h_val, off_val, vals[3]))
            else:
                shape = (vals[1] or "RECT").upper()
                if shape == "CIRCLE":
                    self.db.upsert_unit(
                        ExcavationUnit(
                            name=vals[0], shape="CIRCLE",
                            center_x=float(vals[2]), center_y=float(vals[3]), radius=float(vals[4]),
                        )
                    )
                elif shape == "NOLIMITS":
                    self.db.upsert_unit(
                        ExcavationUnit(
                            name=vals[0], shape="NOLIMITS",
                            min_x=-99999.0, min_y=-99999.0, max_x=99999.0, max_y=99999.0,
                        )
                    )
                else:
                    min_x, min_y = float(vals[2]), float(vals[3])
                    max_x, max_y = float(vals[4]), float(vals[5])
                    if min_x > max_x:
                        raise ValueError("Invalid range: X1 corner is lower left boundary in plan view.")
                    if min_y > max_y:
                        raise ValueError("Invalid range: Y1 corner is lower left boundary in plan view.")
                    self.db.upsert_unit(
                        ExcavationUnit(
                            name=vals[0], shape="RECT",
                            min_x=min_x, min_y=min_y, max_x=max_x, max_y=max_y,
                        )
                    )
            self._reload_table()
        except Exception as exc:
            QMessageBox.warning(self, "Invalid Input", str(exc))

    def _on_delete(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        name_item = self.table.item(row, 0)
        if not name_item:
            return
        name = name_item.text()
        if self.mode == "DATUMS":
            self.db.delete_datum(name)
        elif self.mode == "PRISMS":
            self.db.delete_prism(name)
        else:
            self.db.delete_unit(name)
        self._reload_table()


class FieldFilterDialog(QDialog):
    """
    Replicates frmFilter (0x00439638 / FUN_00501b90 - 'Set Grid Filter'):
    - Left list ('Fields' / lstVars): All core columns and defined .CFG fields
    - Right list ('Values' / lstVals): Distinct recorded values for the selected field
      ('select distinct [<field>] from [<PointTable>]' @ 0x00437f9c)
    - Enforces 'Select field and value to filter by.' (0x00437f38) and alerts
      'No values currently in this table' (0x00437fdc) if empty.
    """

    CORE_FILTER_FIELDS = ["UNIT", "ID", "SUFFIX", "PRISM"]

    def __init__(
        self,
        db: EDMDatabase,
        cfg: EDMConfig,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set Grid Filter")
        self.resize(520, 380)
        self.db = db
        self.cfg = cfg
        self.selected_field: Optional[str] = None
        self.selected_value: Optional[str] = None

        root = QVBoxLayout(self)

        lists_row = QHBoxLayout()

        left_col = QVBoxLayout()
        left_col.addWidget(QLabel("Fields"))
        self.lst_vars = QListWidget()
        self.lst_vars.currentTextChanged.connect(self._on_field_selected)
        left_col.addWidget(self.lst_vars, 1)
        lists_row.addLayout(left_col, 1)

        right_col = QVBoxLayout()
        right_col.addWidget(QLabel("Values"))
        self.lst_vals = QListWidget()
        self.lst_vals.itemDoubleClicked.connect(lambda _: self._on_set_filter())
        right_col.addWidget(self.lst_vals, 1)
        lists_row.addLayout(right_col, 1)

        root.addLayout(lists_row, 1)

        btn_row = QHBoxLayout()
        self.btn_set = QPushButton("Set Filter")
        self.btn_set.clicked.connect(self._on_set_filter)
        self.btn_clear = QPushButton("Show All (Clear Filter)")
        self.btn_clear.clicked.connect(self._on_clear_filter)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_set)
        btn_row.addWidget(self.btn_clear)
        btn_row.addWidget(self.btn_cancel)
        root.addLayout(btn_row)

        self._populate_fields()

    def _populate_fields(self) -> None:
        self.lst_vars.clear()
        seen: List[str] = []
        for f in self.cfg.fields:
            if f.name not in seen:
                seen.append(f.name)
        for core_f in self.CORE_FILTER_FIELDS:
            if all(s.upper() != core_f for s in seen):
                seen.append(core_f)
        self.lst_vars.addItems(seen)
        if seen:
            self.lst_vars.setCurrentRow(0)

    def _on_field_selected(self, field_name: str) -> None:
        self.lst_vals.clear()
        clean = field_name.strip()
        if not clean:
            return
        distinct_vals = self.db.get_distinct_field_values(clean)
        for val in distinct_vals:
            self.lst_vals.addItem(val)
        if distinct_vals:
            self.lst_vals.setCurrentRow(0)

    def select_filter(self, field_name: str, field_value: str) -> None:
        """
        Programmatic helper matching frmFilter validation (0x00437f38):
        Raises ValueError('Select field and value to filter by.') if either is empty.
        """
        f_clean = field_name.strip()
        v_clean = field_value.strip()
        if not f_clean or not v_clean:
            raise ValueError("Select field and value to filter by.")
        self.selected_field = f_clean
        self.selected_value = v_clean

    def _on_set_filter(self) -> None:
        var_item = self.lst_vars.currentItem()
        val_item = self.lst_vals.currentItem()
        if self.lst_vals.count() == 0:
            QMessageBox.information(
                self, "Empty Field", "No values currently in this table"
            )
            return
        f_name = var_item.text().strip() if var_item else ""
        f_val = val_item.text().strip() if val_item else ""
        try:
            self.select_filter(f_name, f_val)
            self.accept()
        except ValueError as exc:
            QMessageBox.warning(self, "Filter Selection", str(exc))

    def _on_clear_filter(self) -> None:
        self.selected_field = None
        self.selected_value = None
        self.accept()


class DataGridDialog(QDialog):
    """
    Replicates frmDataGrid (0x0043b8f4 / FUN_005039a0 - 'Data'):
    - Left panel ('Display Fields' / lstFields): Checkable list of all core & custom .CFG columns
      allowing the user to dynamically show/hide columns (ParseGridFields / FormatGrid).
    - Right panel ('DataGrid' / MSDataGridLib.DataGrid): Full spreadsheet view of recorded points
      supporting direct inline cell editing (automatically committed to SQLite via update_point_cell).
    - Top/Bottom controls:
      * 'Filter by ...' button (opens FieldFilterDialog / frmFilter)
      * 'Clear Filter (Show All)' button
      * 'Show only Points from Current Station' checkbox (replicates mnuFilter @ 0x0041103a)
      * 'Delete Selected Row' button (with series resequencing)
      * 'Hit ESCAPE to return to main form for editing' label (0x0043bc40)
    """

    BASE_COLUMNS = [
        "RecNo",
        "UNIT",
        "ID",
        "SUFFIX",
        "X",
        "Y",
        "Z",
        "PRISM",
        "PRISM_HT",
        "SLOPED",
        "HANGLE",
        "VANGLE",
        "TIMESTAMP",
    ]

    def __init__(
        self,
        db: EDMDatabase,
        cfg: EDMConfig,
        filter_current_station_only: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Data (Show Data Grid)")
        self.resize(1080, 620)
        self.db = db
        self.cfg = cfg
        self.active_filter_field: Optional[str] = None
        self.active_filter_value: Optional[str] = None
        self.filter_current_station_only: bool = filter_current_station_only
        self._suppress_cell_signal: bool = False

        # Build complete ordered list of available columns
        self.all_columns: List[str] = list(self.BASE_COLUMNS)
        for f in self.cfg.fields:
            if all(c.upper() != f.name.upper() for c in self.all_columns):
                self.all_columns.append(f.name)
        for p in self.db.list_points():
            for k in p.get("extra_fields", {}).keys():
                if all(c.upper() != k.upper() for c in self.all_columns):
                    self.all_columns.append(k)

        root = QVBoxLayout(self)

        # Top toolbar: Filter status, Current Station filter checkbox, and instructions
        top_bar = QHBoxLayout()
        self.lbl_filter_status = QLabel("Filter: None (Showing all records)")
        self.chk_station_filter = QCheckBox("Show only Points from Current Station")
        self.chk_station_filter.setChecked(self.filter_current_station_only)
        self.chk_station_filter.toggled.connect(self._on_station_filter_toggled)

        top_bar.addWidget(self.lbl_filter_status, 1)
        top_bar.addWidget(self.chk_station_filter)
        root.addLayout(top_bar)

        # Body split: Left = Display Fields checklist, Right = Editable DataGrid
        body = QHBoxLayout()

        left_box = QGroupBox("Display Fields")
        left_layout = QVBoxLayout(left_box)
        left_box.setMaximumWidth(230)
        self.lst_fields = QListWidget()
        for col_name in self.all_columns:
            item = QListWidgetItem(col_name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.lst_fields.addItem(item)
        self.lst_fields.itemChanged.connect(self._on_display_field_toggled)
        left_layout.addWidget(self.lst_fields, 1)

        btn_filter_by = QPushButton("Filter by ...")
        btn_filter_by.clicked.connect(self._on_open_filter_dialog)
        btn_clear_filter = QPushButton("Show All Records")
        btn_clear_filter.clicked.connect(self.clear_filter)
        left_layout.addWidget(btn_filter_by)
        left_layout.addWidget(btn_clear_filter)
        body.addWidget(left_box)

        right_box = QGroupBox("Point Data Grid (Double-Click Any Cell to Edit Inline)")
        right_layout = QVBoxLayout(right_box)
        self.grid = QTableWidget()
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.grid.horizontalHeader().setStretchLastSection(True)
        self.grid.cellChanged.connect(self._on_cell_edited)
        right_layout.addWidget(self.grid, 1)
        body.addWidget(right_box, 1)

        root.addLayout(body, 1)

        # Footer: Escape hint + Delete Row + Close
        footer = QHBoxLayout()
        lbl_esc = QLabel("Hit ESCAPE to return to main form for editing")
        btn_delete_row = QPushButton("Delete Selected Row")
        btn_delete_row.clicked.connect(self._on_delete_selected_row)
        btn_close = QPushButton("Close [ESC]")
        btn_close.clicked.connect(self.accept)
        footer.addWidget(lbl_esc, 1)
        footer.addWidget(btn_delete_row)
        footer.addWidget(btn_close)
        root.addLayout(footer)

        self.reload_grid()

    def _on_station_filter_toggled(self, checked: bool) -> None:
        self.filter_current_station_only = checked
        self.reload_grid()

    def apply_filter(self, field_name: Optional[str], field_value: Optional[str]) -> None:
        """Applies a field=value filter and reloads the DataGrid."""
        self.active_filter_field = field_name
        self.active_filter_value = field_value
        self.reload_grid()

    def clear_filter(self) -> None:
        """Clears any active field=value filter and reloads all records."""
        self.active_filter_field = None
        self.active_filter_value = None
        self.reload_grid()

    def _on_open_filter_dialog(self) -> None:
        dlg = FieldFilterDialog(self.db, self.cfg, self)
        if dlg.exec() == FieldFilterDialog.Accepted:
            self.apply_filter(dlg.selected_field, dlg.selected_value)

    def set_column_checked(self, col_name: str, checked: bool) -> None:
        """Programmatically checks or unchecks a column in Display Fields (lstFields)."""
        for i in range(self.lst_fields.count()):
            item = self.lst_fields.item(i)
            if item and item.text().upper() == col_name.strip().upper():
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                break
        self._apply_column_visibility()

    def _on_display_field_toggled(self, _item: QListWidgetItem) -> None:
        self._apply_column_visibility()

    def _apply_column_visibility(self) -> None:
        for idx in range(self.lst_fields.count()):
            item = self.lst_fields.item(idx)
            if item is not None and idx < self.grid.columnCount():
                is_visible = item.checkState() == Qt.Checked
                self.grid.setColumnHidden(idx, not is_visible)

    def _extract_cell_text(self, pt: Dict[str, Any], col_name: str) -> str:
        c_up = col_name.upper()
        if c_up == "RECNO":
            return str(pt["doc_id"])
        if c_up == "UNIT":
            return str(pt.get("unit", ""))
        if c_up == "ID":
            return str(pt.get("id", ""))
        if c_up == "SUFFIX":
            return str(pt.get("suffix", 0))
        if c_up == "X":
            return f"{float(pt.get('x', 0.0)):.4f}"
        if c_up == "Y":
            return f"{float(pt.get('y', 0.0)):.4f}"
        if c_up == "Z":
            return f"{float(pt.get('z', 0.0)):.4f}"
        if c_up == "PRISM":
            return str(pt.get("prism_name", ""))
        if c_up == "PRISM_HT":
            return f"{float(pt.get('prism_height', 0.0)):.3f}"
        if c_up == "SLOPED":
            return f"{float(pt.get('slope_dist', 0.0)):.4f}"
        if c_up == "HANGLE":
            return f"{float(pt.get('h_angle_deg', 0.0)):.4f}"
        if c_up == "VANGLE":
            return f"{float(pt.get('v_angle_deg', 0.0)):.4f}"
        if c_up == "TIMESTAMP":
            return str(pt.get("timestamp", ""))

        extra = pt.get("extra_fields", {})
        for k, v in extra.items():
            if k.upper() == c_up:
                return str(v)
        return ""

    def reload_grid(self) -> None:
        stn_xyz: Optional[Tuple[float, float, float]] = None
        if self.filter_current_station_only:
            stn = self.db.load_station_state()
            stn_xyz = (stn.x, stn.y, stn.instrument_center_z)

        pts = self.db.filter_points(
            field_name=self.active_filter_field,
            field_value=self.active_filter_value,
            station_xyz=stn_xyz,
        )

        filter_parts: List[str] = []
        if self.active_filter_field and self.active_filter_value is not None:
            filter_parts.append(f"{self.active_filter_field} = '{self.active_filter_value}'")
        if self.filter_current_station_only:
            filter_parts.append("Current Station Only")
        filter_desc = " AND ".join(filter_parts) if filter_parts else "None (All Records)"
        self.lbl_filter_status.setText(f"Filter: {filter_desc} | Showing {len(pts)} record(s)")

        self._suppress_cell_signal = True
        try:
            self.grid.clear()
            self.grid.setColumnCount(len(self.all_columns))
            self.grid.setHorizontalHeaderLabels(self.all_columns)
            self.grid.setRowCount(len(pts))

            for r, p in enumerate(pts):
                doc_id = int(p["doc_id"])
                for c, col_name in enumerate(self.all_columns):
                    val_str = self._extract_cell_text(p, col_name)
                    cell = QTableWidgetItem(val_str)
                    cell.setData(Qt.UserRole, doc_id)
                    # RecNo and TIMESTAMP are read-only primary/audit keys
                    if col_name.upper() in ("RECNO", "TIMESTAMP"):
                        cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                    self.grid.setItem(r, c, cell)

            self._apply_column_visibility()
        finally:
            self._suppress_cell_signal = False

    def edit_cell_programmatically(self, row: int, col_name: str, new_text: str) -> None:
        """Programmatic helper for testing inline cell editing."""
        col_idx = next(
            (i for i, name in enumerate(self.all_columns) if name.upper() == col_name.strip().upper()),
            -1,
        )
        if col_idx < 0 or not (0 <= row < self.grid.rowCount()):
            raise ValueError(f"Invalid grid cell ({row}, {col_name!r})")
        item = self.grid.item(row, col_idx)
        if item is None:
            raise ValueError("Grid item not initialized")
        item.setText(str(new_text))

    def _on_cell_edited(self, row: int, col: int) -> None:
        if self._suppress_cell_signal:
            return
        item = self.grid.item(row, col)
        if item is None or not (0 <= col < len(self.all_columns)):
            return
        doc_id = item.data(Qt.UserRole)
        if doc_id is None:
            return
        col_name = self.all_columns[col]
        new_val = item.text().strip()

        try:
            # Support inline arithmetic expressions (surf3-edm do_coordinate_math) in coordinate/prism cells
            if col_name.upper() in ("X", "Y", "Z", "PRISM_HT", "SLOPED", "HANGLE", "VANGLE"):
                from survey.math_engine import evaluate_coordinate_math
                eval_val = evaluate_coordinate_math(new_val)
                new_val = f"{eval_val:.4f}" if col_name.upper() != "PRISM_HT" else f"{eval_val:.3f}"
                self._suppress_cell_signal = True
                try:
                    item.setText(new_val)
                finally:
                    self._suppress_cell_signal = False

            # Validate numeric columns before committing
            for f in self.cfg.fields:
                if f.name.upper() == col_name.upper() and f.field_type.upper() in ("NUMERIC", "INTEGER"):
                    float(new_val)
            if col_name.upper() in ("X", "Y", "Z", "SUFFIX", "PRISM_HT", "SLOPED", "HANGLE", "VANGLE"):
                float(new_val)

            self.db.update_point_cell(int(doc_id), col_name, new_val)
            if col_name.upper() in ("PRISM", "PRISM_NAME", "PRISM_HT", "PRISM_HEIGHT"):
                self.reload_grid()
        except ValueError as exc:
            QMessageBox.warning(
                self,
                "Invalid Cell Value",
                f"This field requires valid input ({exc}). Reverting edit.",
            )
            self.reload_grid()

    def _on_delete_selected_row(self) -> None:
        row = self.grid.currentRow()
        if row < 0:
            return
        rec_item = self.grid.item(row, 0)
        if rec_item is None:
            return
        doc_id = int(rec_item.data(Qt.UserRole))
        ans = QMessageBox.question(
            self,
            "Delete Record",
            f"Warning: Deleting will permanently remove Record #{doc_id}. Continue anyway?",
        )
        if ans == QMessageBox.Yes:
            self.db.delete_point_with_resequencing(doc_id, delete_entire_series=False)
            self.reload_grid()


class ContextDefaultsDialog(QDialog):
    """Replicates frmSubUnits (0x0043a5e0 / FUN_0052def0 - 'Context-Dependent Defaults')."""

    def __init__(
        self,
        db: EDMDatabase,
        cfg: EDMConfig,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Context-Dependent Defaults")
        self.resize(680, 480)
        self.db = db
        self.cfg = cfg

        ctx = self.db.load_context_defaults()
        self.master_var: str = ctx["master_var"]
        self.slave_vars: List[str] = list(ctx["slave_vars"])
        self.rules: Dict[str, Dict[str, str]] = copy.deepcopy(ctx["rules"])
        self._suppress_signals: bool = False

        root = QVBoxLayout(self)
        desc = QLabel(
            "Setting Context Dependent Defaults allows you to associate specific values "
            "for the Dependent variables with different values of the Master Variable."
        )
        desc.setWordWrap(True)
        root.addWidget(desc)

        top_split = QHBoxLayout()

        left_box = QGroupBox("Master Variable")
        left_layout = QVBoxLayout(left_box)
        self.cmb_master = QComboBox()
        field_names = [
            f.name for f in self.cfg.fields if f.name.upper() not in ("ID", "SUFFIX")
        ]
        self.cmb_master.addItems(field_names)
        if self.master_var and self.master_var in field_names:
            self.cmb_master.setCurrentText(self.master_var)
        elif field_names:
            self.master_var = field_names[0]
        self.cmb_master.currentTextChanged.connect(self._on_master_changed)
        left_layout.addWidget(QLabel("Master Variable:"))
        left_layout.addWidget(self.cmb_master)

        left_layout.addWidget(QLabel("Master Value (e.g. Level '2A' or Unit 'N100E100'):"))
        self.cmb_master_val = QComboBox()
        self.cmb_master_val.setEditable(True)
        self.cmb_master_val.currentTextChanged.connect(self._reload_rule_table)
        left_layout.addWidget(self.cmb_master_val)
        left_layout.addStretch()
        top_split.addWidget(left_box, 1)

        right_box = QGroupBox("Dependent Variables")
        right_layout = QVBoxLayout(right_box)
        self.lst_slaves = QListWidget()
        self.lst_slaves.itemChanged.connect(self._on_slave_toggled)
        right_layout.addWidget(self.lst_slaves, 1)
        top_split.addWidget(right_box, 1)

        root.addLayout(top_split, 1)

        rule_box = QGroupBox("Associated Default Values for Selected Master Value")
        rule_layout = QVBoxLayout(rule_box)
        self.tbl_rules = QTableWidget()
        self.tbl_rules.setColumnCount(2)
        self.tbl_rules.setHorizontalHeaderLabels(["Dependent Variable", "Default Value"])
        self.tbl_rules.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_rules.cellChanged.connect(self._on_rule_cell_edited)
        rule_layout.addWidget(self.tbl_rules, 1)
        root.addWidget(rule_box, 1)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("OK (Save)")
        ok_btn.clicked.connect(self._on_save)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._on_clear)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addWidget(cancel_btn)
        root.addLayout(btn_row)

        self._populate_slave_list()
        self._populate_master_values()
        self._reload_rule_table()

    def _populate_slave_list(self) -> None:
        self._suppress_signals = True
        try:
            self.lst_slaves.clear()
            for f in self.cfg.fields:
                if f.name.upper() in ("ID", "SUFFIX", self.master_var.upper()):
                    continue
                item = QListWidgetItem(f.name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                is_checked = any(s.upper() == f.name.upper() for s in self.slave_vars)
                item.setCheckState(Qt.Checked if is_checked else Qt.Unchecked)
                self.lst_slaves.addItem(item)
        finally:
            self._suppress_signals = False

    def _populate_master_values(self) -> None:
        self._suppress_signals = True
        try:
            self.cmb_master_val.clear()
            vals: List[str] = []
            for f in self.cfg.fields:
                if f.name.upper() == self.master_var.upper():
                    vals.extend(f.menu_items)
                    break
            for dv in self.db.get_distinct_field_values(self.master_var):
                if dv not in vals:
                    vals.append(dv)
            for rk in self.rules.keys():
                if rk not in vals:
                    vals.append(rk)
            if not vals:
                vals = ["DEFAULT"]
            self.cmb_master_val.addItems(vals)
        finally:
            self._suppress_signals = False

    def _on_master_changed(self, new_master: str) -> None:
        if self._suppress_signals:
            return
        self.master_var = new_master.strip()
        self.slave_vars = [
            s for s in self.slave_vars if s.upper() != self.master_var.upper()
        ]
        self._populate_slave_list()
        self._populate_master_values()
        self._reload_rule_table()

    def _on_slave_toggled(self, _item: QListWidgetItem) -> None:
        if self._suppress_signals:
            return
        checked: List[str] = []
        for i in range(self.lst_slaves.count()):
            it = self.lst_slaves.item(i)
            if it and it.checkState() == Qt.Checked:
                checked.append(it.text())
        self.slave_vars = checked
        self._reload_rule_table()

    def set_slave_checked(self, field_name: str, checked: bool) -> None:
        for i in range(self.lst_slaves.count()):
            it = self.lst_slaves.item(i)
            if it and it.text().upper() == field_name.strip().upper():
                it.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                break

    def set_rule_value(self, master_val: str, slave_var: str, default_val: str) -> None:
        m_key = master_val.strip().upper()
        if not m_key:
            return
        if all(s.upper() != slave_var.strip().upper() for s in self.slave_vars):
            self.slave_vars.append(slave_var.strip())
            self._populate_slave_list()
        entry = dict(self.rules.get(m_key, {}))
        entry[slave_var.strip()] = default_val.strip()
        self.rules[m_key] = entry
        self._reload_rule_table()

    def _reload_rule_table(self) -> None:
        self._suppress_signals = True
        try:
            m_key = self.cmb_master_val.currentText().strip().upper()
            entry = self.rules.get(m_key, {})
            self.tbl_rules.setRowCount(len(self.slave_vars))
            for r, sv in enumerate(self.slave_vars):
                name_item = QTableWidgetItem(sv)
                name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
                val_item = QTableWidgetItem(entry.get(sv, ""))
                self.tbl_rules.setItem(r, 0, name_item)
                self.tbl_rules.setItem(r, 1, val_item)
        finally:
            self._suppress_signals = False

    def _on_rule_cell_edited(self, row: int, col: int) -> None:
        if self._suppress_signals or col != 1:
            return
        m_key = self.cmb_master_val.currentText().strip().upper()
        if not m_key or not (0 <= row < len(self.slave_vars)):
            return
        sv = self.slave_vars[row]
        val_item = self.tbl_rules.item(row, 1)
        new_val = val_item.text().strip() if val_item else ""
        entry = dict(self.rules.get(m_key, {}))
        entry[sv] = new_val
        self.rules[m_key] = entry

    def _on_clear(self) -> None:
        ans = QMessageBox.question(
            self,
            "Clear Context Defaults",
            "This will clear default values stored in the database. Proceed anyway?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans == QMessageBox.Yes:
            self.db.clear_context_defaults()
            self.master_var = ""
            self.slave_vars.clear()
            self.rules.clear()
            self.accept()

    def _on_save(self) -> None:
        if not self.slave_vars:
            ans = QMessageBox.question(
                self,
                "No Dependent Variables",
                "No dependent variables have been checked. Do you want to clear this function?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if ans == QMessageBox.Yes:
                self.db.clear_context_defaults()
                self.accept()
            return
        self.db.save_context_defaults(self.master_var, self.slave_vars, self.rules)
        self.accept()


class ManualShotDialog(QDialog):
    """Replicates frmManualshot (0x0043b17c / FUN_004e8730 - 'Record point')."""

    def __init__(
        self,
        default_prism_height: float = 0.0,
        default_mode: str = "ANGLES",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Record point (Manual Entry)")
        self.resize(440, 320)
        self.observation: Optional[RawObservation] = None
        self.prism_height: float = default_prism_height

        root = QVBoxLayout(self)

        mode_box = QGroupBox("Manual Input Mode")
        mode_row = QHBoxLayout(mode_box)
        self.mode_group = QButtonGroup(self)
        self.rb_angles = QRadioButton("Vertical angle, Horizontal Angle and Distance")
        self.rb_xyz = QRadioButton("X, Y and Z coordinates")
        self.mode_group.addButton(self.rb_angles)
        self.mode_group.addButton(self.rb_xyz)
        mode_row.addWidget(self.rb_angles)
        mode_row.addWidget(self.rb_xyz)
        root.addWidget(mode_box)

        self.form_widget = QWidget()
        self.form_layout = QFormLayout(self.form_widget)

        self.txt_hangle = QLineEdit()
        self.txt_hangle.setPlaceholderText("ddd.mmss (e.g. 90.0000)")
        self.txt_vangle = QLineEdit()
        self.txt_vangle.setPlaceholderText("ddd.mmss (e.g. 90.0000)")
        self.txt_sloped = QLineEdit()
        self.txt_sloped.setPlaceholderText("meters (e.g. 10.000)")
        self.txt_poleh = QLineEdit(f"{default_prism_height:.3f}")
        self.txt_poleh.setPlaceholderText("meters [-10 to 10]")

        self.txt_x = QLineEdit()
        self.txt_x.setPlaceholderText("meters")
        self.txt_y = QLineEdit()
        self.txt_y.setPlaceholderText("meters")
        self.txt_z = QLineEdit()
        self.txt_z.setPlaceholderText("meters")

        self.lbl_h = QLabel("Horizontal angle (ddd.mmss):")
        self.lbl_v = QLabel("Vertical angle (ddd.mmss):")
        self.lbl_sd = QLabel("Slope distance (meters):")
        self.lbl_ph = QLabel("Prism height (meters):")

        self.lbl_x = QLabel("X coordinate (meters):")
        self.lbl_y = QLabel("Y coordinate (meters):")
        self.lbl_z = QLabel("Z coordinate (meters):")

        self.form_layout.addRow(self.lbl_h, self.txt_hangle)
        self.form_layout.addRow(self.lbl_v, self.txt_vangle)
        self.form_layout.addRow(self.lbl_sd, self.txt_sloped)
        self.form_layout.addRow(self.lbl_ph, self.txt_poleh)
        self.form_layout.addRow(self.lbl_x, self.txt_x)
        self.form_layout.addRow(self.lbl_y, self.txt_y)
        self.form_layout.addRow(self.lbl_z, self.txt_z)
        root.addWidget(self.form_widget, 1)

        self.rb_angles.toggled.connect(self._update_mode_visibility)
        if default_mode.strip().upper() == "XYZ":
            self.rb_xyz.setChecked(True)
        else:
            self.rb_angles.setChecked(True)
        self._update_mode_visibility()

        btn_row = QHBoxLayout()
        accept_btn = QPushButton("&Accept")
        accept_btn.clicked.connect(self._on_accept)
        cancel_btn = QPushButton("&Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(accept_btn)
        btn_row.addWidget(cancel_btn)
        root.addLayout(btn_row)

    def _update_mode_visibility(self) -> None:
        is_angles = self.rb_angles.isChecked()
        for w in (
            self.lbl_h, self.txt_hangle,
            self.lbl_v, self.txt_vangle,
            self.lbl_sd, self.txt_sloped,
            self.lbl_ph, self.txt_poleh,
        ):
            w.setVisible(is_angles)
        for w in (
            self.lbl_x, self.txt_x,
            self.lbl_y, self.txt_y,
            self.lbl_z, self.txt_z,
        ):
            w.setVisible(not is_angles)

    def submit_angles(
        self,
        h_angle_dddmmss: str | float,
        v_angle_dddmmss: str | float,
        slope_dist: str | float,
        prism_height: str | float = 0.0,
    ) -> RawObservation:
        try:
            h_str = str(h_angle_dddmmss).strip()
            v_str = str(v_angle_dddmmss).strip()
            sd_str = str(slope_dist).strip()
            ph_str = str(prism_height).strip() or "0.0"
            if not h_str or not v_str or not sd_str:
                raise ValueError("Enter a vertical angle, horizontal angle, and slope distance.")
            h_val = float(h_str)
            v_val = float(v_str)
            sd_val = float(sd_str)
            ph_val = float(ph_str)
        except ValueError as exc:
            raise ValueError("Enter a vertical angle, horizontal angle, and slope distance.") from exc

        validate_prism_bounds(ph_val, 0.0)
        if sd_val <= 0.0 or not (0.0 <= h_val <= 360.0) or not (0.0 <= v_val <= 360.0):
            raise ValueError("Enter a vertical angle, horizontal angle, and slope distance.")

        hz_rad = dms_encoded_to_radians(h_val) % (2.0 * math.pi)
        v_rad = dms_encoded_to_radians(v_val)
        self.prism_height = ph_val
        self.observation = RawObservation(
            slope_distance=sd_val,
            horizontal_angle_rad=hz_rad,
            vertical_angle_rad=v_rad,
            raw_tx="MANUAL_ANGLES",
            raw_rx=f"H={h_val:.4f} V={v_val:.4f} SD={sd_val:.3f} PH={ph_val:.3f}",
        )
        return self.observation

    def submit_xyz(
        self,
        x_val: str | float,
        y_val: str | float,
        z_val: str | float,
    ) -> RawObservation:
        try:
            xs = str(x_val).strip()
            ys = str(y_val).strip()
            zs = str(z_val).strip()
            if not xs or not ys or not zs:
                raise ValueError("Enter coordinates as numeric values.")
            xf, yf, zf = float(xs), float(ys), float(zs)
        except ValueError as exc:
            raise ValueError("Enter coordinates as numeric values.") from exc

        self.observation = RawObservation(
            slope_distance=0.0,
            horizontal_angle_rad=0.0,
            vertical_angle_rad=math.pi / 2.0,
            direct_xyz=(xf, yf, zf),
            raw_tx="MANUAL_XYZ",
            raw_rx=f"X={xf:.3f} Y={yf:.3f} Z={zf:.3f}",
        )
        return self.observation

    def _on_accept(self) -> None:
        try:
            if self.rb_angles.isChecked():
                self.submit_angles(
                    self.txt_hangle.text(),
                    self.txt_vangle.text(),
                    self.txt_sloped.text(),
                    self.txt_poleh.text(),
                )
            else:
                self.submit_xyz(
                    self.txt_x.text(),
                    self.txt_y.text(),
                    self.txt_z.text(),
                )
            self.accept()
        except ValueError as exc:
            QMessageBox.warning(self, "Manual Entry Error", str(exc))


class EDMStatusDialog(QDialog):
    """
    Replicates frmStatus (0x0043c6f0 / FUN_0050a820 - 'EDM Status'):
    Displays read-only diagnostic fields for:
    - CFG Filename (txtcfgfile)
    - EDM INI Filename (txtinifile)
    - Database (txtdbname)
    - Total Station (txttotalstation)
    - COMPort (txtcomport)
    - Settings (txtsettings)
    - Points Table (txtPointsTable)
    - Station Name (txtstationname)
    - Station Height (txtstationheight) + '(Not necessarily accurate depending on setup type)'
    - Station X, Station Y, Station Z (txtstationx, txtstationy, txtstationz) +
      'All points are relative to this location which is a point at the center of the total station and not the point on the ground.'
    """

    def __init__(
        self,
        db: EDMDatabase,
        cfg: EDMConfig,
        cfg_path: str = "",
        ini_path: str = "edm.ini",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("EDM Status")
        self.resize(560, 480)
        self.db = db
        self.cfg = cfg

        stn = self.db.load_station_state()
        pts_count = len(self.db.list_points())
        settings_str = f"{cfg.baudrate},{cfg.parity},{cfg.bytesize},{cfg.stopbits}"

        root = QVBoxLayout(self)

        sys_group = QGroupBox("Configuration & Communication Status")
        sys_form = QFormLayout(sys_group)

        self.txt_cfg_file = self._ro_line(str(cfg_path or cfg.database_name or "sample_excavation.cfg"))
        self.txt_ini_file = self._ro_line(str(ini_path))
        self.txt_db_name = self._ro_line(str(db.db_path))
        self.txt_total_station = self._ro_line(cfg.station_type)
        self.txt_com_port = self._ro_line(cfg.com_port)
        self.txt_settings = self._ro_line(settings_str)
        self.txt_points_table = self._ro_line(f"{cfg.point_table} ({pts_count} records)")

        sys_form.addRow("CFG Filename :", self.txt_cfg_file)
        sys_form.addRow("EDM INI Filename :", self.txt_ini_file)
        sys_form.addRow("Database :", self.txt_db_name)
        sys_form.addRow("Total Station :", self.txt_total_station)
        sys_form.addRow("COMPort :", self.txt_com_port)
        sys_form.addRow("Settings :", self.txt_settings)
        sys_form.addRow("Points Table :", self.txt_points_table)
        root.addWidget(sys_group)

        stn_group = QGroupBox("Current Station Coordinates (Optical Center)")
        stn_layout = QVBoxLayout(stn_group)
        note_lbl = QLabel(
            "All points are relative to this location which is a point at the center "
            "of the total station and not the point on the ground."
        )
        note_lbl.setWordWrap(True)
        stn_layout.addWidget(note_lbl)

        stn_form = QFormLayout()
        self.txt_station_name = self._ro_line(f"{stn.station_name} [{stn.setup_method}]")
        self.txt_station_x = self._ro_line(f"{stn.x:.3f}")
        self.txt_station_y = self._ro_line(f"{stn.y:.3f}")
        self.txt_station_z = self._ro_line(f"{stn.instrument_center_z:.3f}")
        self.txt_station_height = self._ro_line(
            f"{stn.instrument_height:.3f} (Not necessarily accurate depending on setup type)"
        )

        stn_form.addRow("Station Name :", self.txt_station_name)
        stn_form.addRow("Station X :", self.txt_station_x)
        stn_form.addRow("Station Y :", self.txt_station_y)
        stn_form.addRow("Station Z :", self.txt_station_z)
        stn_form.addRow("Station Height :", self.txt_station_height)
        stn_layout.addLayout(stn_form)
        root.addWidget(stn_group)

        close_btn = QPushButton("OK")
        close_btn.clicked.connect(self.accept)
        root.addWidget(close_btn)

    @staticmethod
    def _ro_line(text: str) -> QLineEdit:
        le = QLineEdit(text)
        le.setReadOnly(True)
        return le


class DebugStationDialog(QDialog):
    """
    Replicates frmDebug (0x0043c19c / FUN_004c8840 / FUN_004cc370 - 'Debug Total Station'):
    - COM Settings display (txtComSettings)
    - Initialize button (btnInitialize)
    - X-Shot button (btnXShot) that triggers a test shot and populates Results:
      H-Angle (txtHangle), V-Angle (txtVangle), Slope-D (txtSloped), Pole H. (txtPoleHeight),
      and reduced X, Y, Z coordinates (txtX, txtY, txtZ).
    - Preset total station command dropdown (ts_commands) + Send (execute_command):
      * '0 Horizontal Angle'
      * 'Get date/time'
      * 'Get Instr. Name'
      * 'Launch measurement'
      * 'Get meas. with tilt'
    - Output log (txtOutput) + Clear (cmdClear) + Copy to Clipboard (Command1)
    """

    PRESET_COMMANDS = [
        "0 Horizontal Angle",
        "Get date/time",
        "Get Instr. Name",
        "Launch measurement",
        "Get meas. with tilt",
    ]

    def __init__(
        self,
        db: EDMDatabase,
        cfg: EDMConfig,
        driver: TotalStationDriver,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Debug Total Station")
        self.resize(700, 540)
        self.db = db
        self.cfg = cfg
        self.driver = driver
        self._is_initialized: bool = True

        root = QVBoxLayout(self)
        info_lbl = QLabel(
            "This form is not a complete debug routine. Rather it contains code that helps "
            "Shannon track down problems with new and different total stations. Be sure to "
            "first set the COM port and the COM settings on the Select Total Station window."
        )
        info_lbl.setWordWrap(True)
        root.addWidget(info_lbl)

        # Top row: COM Settings + Initialize + X-Shot
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("COM Settings:"))
        settings_str = f"{cfg.station_type} @ {cfg.com_port} ({cfg.baudrate},{cfg.parity},{cfg.bytesize},{cfg.stopbits})"
        self.txt_com_settings = QLineEdit(settings_str)
        self.txt_com_settings.setReadOnly(True)
        btn_init = QPushButton("Initialize")
        btn_init.clicked.connect(self.run_initialize)
        btn_xshot = QPushButton("X-Shot")
        btn_xshot.clicked.connect(self.run_xshot)
        top_row.addWidget(self.txt_com_settings, 1)
        top_row.addWidget(btn_init)
        top_row.addWidget(btn_xshot)
        root.addLayout(top_row)

        # Second row: Preset command dropdown + Send button
        cmd_row = QHBoxLayout()
        cmd_row.addWidget(QLabel("Preset Command:"))
        self.cbo_commands = QComboBox()
        self.cbo_commands.setEditable(True)
        self.cbo_commands.addItems(self.PRESET_COMMANDS)
        btn_send = QPushButton("Send")
        btn_send.clicked.connect(self._on_send_clicked)
        cmd_row.addWidget(self.cbo_commands, 1)
        cmd_row.addWidget(btn_send)
        root.addLayout(cmd_row)

        # Middle split: Output log on left, Results frame on right
        mid_split = QHBoxLayout()

        log_box = QGroupBox("Serial Output Log")
        log_layout = QVBoxLayout(log_box)
        self.txt_output = QPlainTextEdit()
        self.txt_output.setReadOnly(True)
        log_layout.addWidget(self.txt_output, 1)

        log_btns = QHBoxLayout()
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self.txt_output.clear)
        btn_copy = QPushButton("Copy to Clipboard")
        btn_copy.clicked.connect(self._on_copy_clipboard)
        log_btns.addWidget(btn_clear)
        log_btns.addWidget(btn_copy)
        log_layout.addLayout(log_btns)
        mid_split.addWidget(log_box, 3)

        res_box = QGroupBox("Results")
        res_form = QFormLayout(res_box)
        self.txt_hangle = QLineEdit()
        self.txt_vangle = QLineEdit()
        self.txt_sloped = QLineEdit()
        self.txt_pole_height = QLineEdit("0.000")
        self.txt_x = QLineEdit()
        self.txt_y = QLineEdit()
        self.txt_z = QLineEdit()
        for le in (self.txt_hangle, self.txt_vangle, self.txt_sloped, self.txt_x, self.txt_y, self.txt_z):
            le.setReadOnly(True)

        res_form.addRow("H-Angle:", self.txt_hangle)
        res_form.addRow("V-Angle:", self.txt_vangle)
        res_form.addRow("Slope-D:", self.txt_sloped)
        res_form.addRow("Pole H.:", self.txt_pole_height)
        res_form.addRow("X:", self.txt_x)
        res_form.addRow("Y:", self.txt_y)
        res_form.addRow("Z:", self.txt_z)
        mid_split.addWidget(res_box, 2)

        root.addLayout(mid_split, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        root.addWidget(close_btn)

    def run_initialize(self) -> str:
        """Replicates btnInitialize on frmDebug."""
        res = self.driver.initialize_station_comm()
        self._is_initialized = True
        self.txt_output.appendPlainText(f"[Initialize] -> {res}")
        return res

    def run_xshot(self) -> RawObservation:
        """Replicates btnXShot on frmDebug: measures a shot and populates H-Angle, V-Angle, Slope-D, X, Y, Z."""
        stn = self.db.load_station_state()
        try:
            pole_h = float(self.txt_pole_height.text().strip() or "0.0")
        except ValueError:
            pole_h = 0.0

        obs = self.driver.measure()
        pt, _, _ = reduce_polar_to_cartesian(
            station=stn,
            slope_distance=obs.slope_distance,
            horizontal_angle_rad=obs.horizontal_angle_rad,
            vertical_angle_rad=obs.vertical_angle_rad,
            prism_height=pole_h,
            prism_offset=0.0,
        )
        self.txt_hangle.setText(f"{obs.horizontal_angle_dddmmss:.4f}")
        self.txt_vangle.setText(f"{obs.vertical_angle_dddmmss:.4f}")
        self.txt_sloped.setText(f"{obs.slope_distance:.3f}")
        self.txt_x.setText(f"{pt.x:.3f}")
        self.txt_y.setText(f"{pt.y:.3f}")
        self.txt_z.setText(f"{pt.z:.3f}")
        self.txt_output.appendPlainText(
            f"[X-Shot] TX={obs.raw_tx!r} | RX={obs.raw_rx!r} -> "
            f"X={pt.x:.3f} Y={pt.y:.3f} Z={pt.z:.3f}"
        )
        return obs

    def execute_preset_command(self, command_label: str) -> str:
        """
        Replicates execute_command on frmDebug (FUN_004cc370 / FUN_004cc480 / FUN_004cc5a0 / FUN_004cc6b0):
        Validates command_label against the 5 GeoCOM/EDMWin debug presets or raises
        ValueError('Error: Unrecognize command selected.') / ValueError('Select a command from the dropdown next to this button.').
        """
        cmd_clean = command_label.strip()
        if not cmd_clean:
            raise ValueError("Select a command from the dropdown next to this button.")

        wire_map = {
            "0 HORIZONTAL ANGLE": self.driver.format_set_horizontal_angle_cmd(0.0) or "%R1Q,2113:0.0000000\r\n",
            "GET DATE/TIME": "%R1Q,5008:\r\n",
            "GET INSTR. NAME": "%R1Q,5004,6,21341:\r\n",
            "LAUNCH MEASUREMENT": "%R1Q,2008:1,1\r\n",
            "GET MEAS. WITH TILT": "%R1Q,2187:10000,1\r\n",
        }
        key = cmd_clean.upper()
        if key not in wire_map:
            raise ValueError("Error: Unrecognize command selected.")

        wire_cmd = wire_map[key]
        if hasattr(self.driver, "is_connected") and self.driver.is_connected and self.driver._serial is None:
            # Simulator mode: return synthetic GeoCOM reply
            sim_reply = f"%R1P,0,0:0 (SIMULATED REPLY FOR {wire_cmd.strip()})"
            self.txt_output.appendPlainText(f"Sent-> {wire_cmd.strip()}\nReceived-> {sim_reply}")
            return sim_reply

        rx = self.driver.send_raw(wire_cmd)
        self.txt_output.appendPlainText(f"Sent-> {wire_cmd.strip()}\nReceived-> {rx.strip()}")
        return rx

    def _on_send_clicked(self) -> None:
        try:
            self.execute_preset_command(self.cbo_commands.currentText())
        except Exception as exc:
            QMessageBox.warning(self, "Debug Command Error", str(exc))

    def _on_copy_clipboard(self) -> None:
        from PySide6.QtWidgets import QApplication
        clip = QApplication.clipboard()
        if clip is not None:
            clip.setText(self.txt_output.toPlainText())


class AboutDialog(QDialog):
    """
    Replicates frmAbout (0x00439808 - 'About EDM'):
    Displays EDM 3.2 / Modern EDM 3.3.0 credits for Harold L. Dibble and Shannon JP. McPherron.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About EDM")
        self.resize(380, 220)

        layout = QVBoxLayout(self)
        title = QLabel("EDM 3.2 / Modern EDM 3.3.0")
        title.setAlignment(Qt.AlignCenter)
        authors = QLabel("Harold L. Dibble\nShannon JP. McPherron")
        authors.setAlignment(Qt.AlignCenter)
        copy_lbl = QLabel("Copyright (c) 2023 OldStoneAge.com\nModern Cross-Platform Python/PySide6 Edition")
        copy_lbl.setAlignment(Qt.AlignCenter)

        layout.addWidget(title)
        layout.addWidget(authors)
        layout.addWidget(copy_lbl)

        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        layout.addWidget(ok_btn)


class SetupLogFilesDialog(QDialog):
    """
    Replicates frmSetupLogFiles (0x0043ae4c / FUN_00557960 - 'Setup Log Files'):
    - chkTSLog ('Log total station communications')
    - txtTSLog (TSLogFile path; double-click or Browse button opens file dialog)
    - cmdClearTSLog ('Clear file' - truncates TSLogFile)
    - chkGeneralLog ('Log setups and other program options')
    - txtGeneralLog (GeneralLogFile path; double-click or Browse button opens file dialog)
    - cmdClearGeneralLog ('Clear file' - truncates GeneralLogFile)
    """

    def __init__(
        self,
        cfg: EDMConfig,
        default_dir: Optional[Path] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Setup Log Files")
        self.resize(580, 320)
        self.cfg = cfg
        base = default_dir or Path.cwd()

        default_ts = cfg.ts_log_file or str(base / "ts_comm.log")
        default_gen = cfg.general_log_file or str(base / "general_edm.log")

        root = QVBoxLayout(self)

        # Total Station Communications Log Box
        ts_box = QGroupBox("Total Station Communications Log")
        ts_layout = QVBoxLayout(ts_box)
        self.chk_ts_log = QCheckBox("Log total station communications")
        self.chk_ts_log.setChecked(cfg.use_ts_log)
        ts_layout.addWidget(self.chk_ts_log)

        ts_row = QHBoxLayout()
        self.txt_ts_log = QLineEdit(default_ts)
        self.txt_ts_log.setToolTip("(double click in the box or click Browse to open a file browser)")
        btn_browse_ts = QPushButton("Browse...")
        btn_browse_ts.clicked.connect(self._browse_ts_log)
        btn_clear_ts = QPushButton("Clear file")
        btn_clear_ts.clicked.connect(self.clear_ts_log_file)
        ts_row.addWidget(self.txt_ts_log, 1)
        ts_row.addWidget(btn_browse_ts)
        ts_row.addWidget(btn_clear_ts)
        ts_layout.addLayout(ts_row)
        ts_layout.addWidget(QLabel("(double click in the box or click Browse to open a file browser)"))
        root.addWidget(ts_box)

        # General Setups & Options Log Box
        gen_box = QGroupBox("General Program & Setup Log")
        gen_layout = QVBoxLayout(gen_box)
        self.chk_general_log = QCheckBox("Log setups and other program options")
        self.chk_general_log.setChecked(cfg.use_general_log)
        gen_layout.addWidget(self.chk_general_log)

        gen_row = QHBoxLayout()
        self.txt_general_log = QLineEdit(default_gen)
        self.txt_general_log.setToolTip("(double click in the box or click Browse to open a file browser)")
        btn_browse_gen = QPushButton("Browse...")
        btn_browse_gen.clicked.connect(self._browse_general_log)
        btn_clear_gen = QPushButton("Clear file")
        btn_clear_gen.clicked.connect(self.clear_general_log_file)
        gen_row.addWidget(self.txt_general_log, 1)
        gen_row.addWidget(btn_browse_gen)
        gen_row.addWidget(btn_clear_gen)
        gen_layout.addLayout(gen_row)
        gen_layout.addWidget(QLabel("(double click in the box or click Browse to open a file browser)"))
        root.addWidget(gen_box)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("OK (Save)")
        ok_btn.clicked.connect(self._on_save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        root.addLayout(btn_row)

    def _browse_ts_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Select Total Station Log File", self.txt_ts_log.text(), "Log Files (*.log *.txt)"
        )
        if path:
            self.txt_ts_log.setText(path)

    def _browse_general_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Select General Log File", self.txt_general_log.text(), "Log Files (*.log *.txt)"
        )
        if path:
            self.txt_general_log.setText(path)

    def clear_ts_log_file(self) -> None:
        """Replicates cmdClearTSLog ('Clear file') on frmSetupLogFiles."""
        p = Path(self.txt_ts_log.text().strip())
        if p.parent.exists():
            p.write_text("", encoding="utf-8")

    def clear_general_log_file(self) -> None:
        """Replicates cmdClearGeneralLog ('Clear file') on frmSetupLogFiles."""
        p = Path(self.txt_general_log.text().strip())
        if p.parent.exists():
            p.write_text("", encoding="utf-8")

    @staticmethod
    def append_log_entry(log_path: str | Path, message: str) -> None:
        """Appends a timestamped line to the specified log file if log_path is non-empty."""
        clean = str(log_path).strip()
        if not clean:
            return
        import datetime
        p = Path(clean)
        p.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message.rstrip()}\n")

    def _on_save(self) -> None:
        self.cfg.use_ts_log = self.chk_ts_log.isChecked()
        self.cfg.ts_log_file = self.txt_ts_log.text().strip()
        self.cfg.use_general_log = self.chk_general_log.isChecked()
        self.cfg.general_log_file = self.txt_general_log.text().strip()
        self.accept()


class CreateDefaultProjectDialog(QDialog):
    """
    Replicates frmDefault (0x0043db0c / FUN_005039a0 - 'Create a Default CFG'):
    - CFG Filename (cfgfiletext, default: Survey.CFG)
    - Database Name (databasetext, default: Survey.sqlite / Survey.MDB)
    - Points Table (pointsfiletext, default: Points)
    - Total Station Type (totalstationmenu: Topcon, Leica, Wild, Wild2, Leica_AutoTilt, Builder, Sokkia, Nikon, Simulation, None)
    - COM Port (comportmenu: COM1..COM20)
    - Baud Rate (baudratemenu: 1200, 2400, 4800, 9600, 19200)
    - Parity (paritymenu: Even, Odd, None)
    - Data Bits (databitmenu: 7, 8)
    - Stop Bits (stopbitmenu: 1, 2)
    - Write (Command1) & Cancel (Command2)
    Enforces exact decompiled validation strings:
    - 'Please supply a CFG filename.' (0x004380c8)
    - 'Please supply a database filename.' (0x00438108)
    - 'Please supply a points table name.' (0x00438154)
    - 'If the total station type is not set to Simulation, the baud rate, parity, data bits and stop bits must be set.' (0x00438264)
    """

    def __init__(
        self,
        default_dir: Optional[Path] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create a Default CFG")
        self.resize(620, 520)
        base = default_dir or Path.cwd()
        self.created_cfg_path: Optional[Path] = None
        self.created_db_path: Optional[Path] = None
        self.created_cfg: Optional[EDMConfig] = None

        root = QVBoxLayout(self)

        intro_lbl = QLabel(
            "This form lets you create a default CFG file and database that can be used to "
            "immediately start recording points. It is based generally on the CFG that we "
            "use for excavating Paleolithic sites.\n\n"
            "Once you press Write, the program will create the CFG and database. An empty points "
            "table will be created as well. The program will add a datums table and a prisms "
            "table with default entries. All of these things can be modified using the menu "
            "options in this program."
        )
        intro_lbl.setWordWrap(True)
        root.addWidget(intro_lbl)

        form = QFormLayout()

        cfg_row = QHBoxLayout()
        self.txt_cfg = QLineEdit(str(base / "Survey.CFG"))
        btn_browse_cfg = QPushButton("Browse...")
        btn_browse_cfg.clicked.connect(self._browse_cfg)
        cfg_row.addWidget(self.txt_cfg, 1)
        cfg_row.addWidget(btn_browse_cfg)

        db_row = QHBoxLayout()
        self.txt_db = QLineEdit(str(base / "Survey.sqlite"))
        btn_browse_db = QPushButton("Browse...")
        btn_browse_db.clicked.connect(self._browse_db)
        db_row.addWidget(self.txt_db, 1)
        db_row.addWidget(btn_browse_db)

        self.txt_points_table = QLineEdit("Points")

        self.cbo_station = QComboBox()
        self.cbo_station.addItems([
            "Simulator",
            "Topcon",
            "Leica",
            "Wild",
            "Wild2",
            "Leica_AutoTilt",
            "Builder",
            "Sokkia",
            "Nikon",
            "None",
        ])

        self.cbo_com = QComboBox()
        self.cbo_com.addItems([f"COM{i}" for i in range(1, 21)])

        self.cbo_baud = QComboBox()
        self.cbo_baud.setEditable(True)
        self.cbo_baud.addItems(["1200", "2400", "4800", "9600", "19200"])
        self.cbo_baud.setCurrentText("1200")

        self.cbo_parity = QComboBox()
        self.cbo_parity.setEditable(True)
        self.cbo_parity.addItems(["Even", "Odd", "None"])
        self.cbo_parity.setCurrentText("Even")

        self.cbo_databits = QComboBox()
        self.cbo_databits.setEditable(True)
        self.cbo_databits.addItems(["7", "8"])
        self.cbo_databits.setCurrentText("7")

        self.cbo_stopbits = QComboBox()
        self.cbo_stopbits.setEditable(True)
        self.cbo_stopbits.addItems(["1", "2"])
        self.cbo_stopbits.setCurrentText("1")

        form.addRow("CFG Filename :", cfg_row)
        form.addRow("Database Name :", db_row)
        form.addRow("Points Table :", self.txt_points_table)
        form.addRow("Total Station Type :", self.cbo_station)
        form.addRow("COM Port :", self.cbo_com)
        form.addRow("Baud Rate :", self.cbo_baud)
        form.addRow("Parity :", self.cbo_parity)
        form.addRow("Data Bits :", self.cbo_databits)
        form.addRow("Stop Bits :", self.cbo_stopbits)
        root.addLayout(form)

        btn_row = QHBoxLayout()
        write_btn = QPushButton("Write")
        write_btn.clicked.connect(self._on_write)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(write_btn)
        btn_row.addWidget(cancel_btn)
        root.addLayout(btn_row)

    def _browse_cfg(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Create New CFG file", self.txt_cfg.text(), "CFG Files (*.cfg *.CFG)"
        )
        if path:
            self.txt_cfg.setText(path)

    def _browse_db(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Create New Database", self.txt_db.text(), "SQLite / Site Files (*.sqlite *.db *.mdb)"
        )
        if path:
            self.txt_db.setText(path)

    def write_default_project(
        self,
        cfg_path_str: Optional[str] = None,
        db_path_str: Optional[str] = None,
        points_table_str: Optional[str] = None,
        station_type_str: Optional[str] = None,
        com_port_str: Optional[str] = None,
        baud_str: Optional[str] = None,
        parity_str: Optional[str] = None,
        databits_str: Optional[str] = None,
        stopbits_str: Optional[str] = None,
    ) -> Tuple[Path, Path, EDMConfig]:
        """
        Programmatic helper matching frmDefault Write (Command1_Click):
        Validates inputs, creates the Paleolithic default .CFG file via save_legacy_cfg_file,
        and initializes the database with default Datums ('Main'), Prisms ('Zero', '10cm'),
        and Units ('Unit1', 'Unit2').
        """
        c_str = (cfg_path_str if cfg_path_str is not None else self.txt_cfg.text()).strip()
        d_str = (db_path_str if db_path_str is not None else self.txt_db.text()).strip()
        pt_str = (points_table_str if points_table_str is not None else self.txt_points_table.text()).strip()
        st_str = (station_type_str if station_type_str is not None else self.cbo_station.currentText()).strip()
        com_str = (com_port_str if com_port_str is not None else self.cbo_com.currentText()).strip()
        b_str = (baud_str if baud_str is not None else self.cbo_baud.currentText()).strip()
        p_str = (parity_str if parity_str is not None else self.cbo_parity.currentText()).strip()
        db_bits = (databits_str if databits_str is not None else self.cbo_databits.currentText()).strip()
        sb_bits = (stopbits_str if stopbits_str is not None else self.cbo_stopbits.currentText()).strip()

        if not c_str:
            raise ValueError("Please supply a CFG filename.")
        if not d_str:
            raise ValueError("Please supply a database filename.")
        if not pt_str:
            raise ValueError("Please supply a points table name.")

        if st_str.upper() not in ("SIMULATOR", "SIMULATION", "NONE"):
            if not (b_str and p_str and db_bits and sb_bits):
                raise ValueError(
                    "If the total station type is not set to Simulation, the baud rate, "
                    "parity, data bits and stop bits must be set."
                )

        baud_val = int(b_str or "1200")
        parity_char = p_str.upper()[0] if p_str else "E"
        bytesize_val = int(db_bits or "7")
        stopbits_val = int(sb_bits or "1")

        cfg_path = Path(c_str)
        db_path = Path(d_str)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        cfg = build_default_paleolithic_cfg(
            cfg_filename=str(cfg_path),
            db_filename=str(db_path),
            points_table=pt_str,
            station_type=st_str,
            com_port=com_str,
            baudrate=baud_val,
            parity=parity_char,
            bytesize=bytesize_val,
            stopbits=stopbits_val,
        )
        save_legacy_cfg_file(cfg, cfg_path)

        # Initialize the new database with frmDefault's exact default entries (0x00438544-0x004385a0):
        # Prisms: 'Zero' (0.0m, 0.0m) and '10cm' (0.10m, 0.0m)
        # Datums: 'Main' (100.0, 100.0, 10.0)
        # Units: 'Unit1' and 'Unit2'
        if db_path.exists():
            db_path.unlink()
        new_db = EDMDatabase(db_path)
        new_db.upsert_datum(Datum("Main", 100.000, 100.000, 10.000, "Default Main Datum"))
        new_db.upsert_prism(Prism("Zero", 0.000, 0.000, "Zero Prism"))
        new_db.upsert_prism(Prism("10cm", 0.100, 0.000, "10cm Pin Pole"))
        new_db.upsert_unit(
            ExcavationUnit("Unit1", shape="RECT", min_x=100.0, min_y=100.0, max_x=101.0, max_y=101.0)
        )
        new_db.upsert_unit(
            ExcavationUnit("Unit2", shape="RECT", min_x=101.0, min_y=100.0, max_x=102.0, max_y=101.0)
        )
        new_db.close()

        self.created_cfg_path = cfg_path
        self.created_db_path = db_path
        self.created_cfg = cfg
        return cfg_path, db_path, cfg

    def _on_write(self) -> None:
        try:
            self.write_default_project()
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "Create Default CFG Error", str(exc))


class LabelPrinterDialog(QDialog):
    """
    Replicates modPrint / Artifact Bag Label Preview, Spool & Reprint Dialog:
    - Select any recorded point (defaults to last recorded point)
    - Switch between Plain-Text ASCII and Zebra ZPL II (Code-39 barcode) formats
    - Configure Site Name header, number of copies (1..20), and Spool File path
    - Live preview of the formatted bag label + Reprint / Spool & Clear Spool actions
    """

    def __init__(
        self,
        db: EDMDatabase,
        cfg: EDMConfig,
        spool_path: Path,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Artifact Bag Label Printer & Reprint")
        self.resize(660, 500)
        self.db = db
        self.cfg = cfg
        self.spool_path = Path(spool_path)
        self.points = self.db.list_points()

        root = QVBoxLayout(self)

        form = QFormLayout()
        self.cbo_shots = QComboBox()
        for p in self.points:
            code_val = p.get("extra_fields", {}).get("CODE", p.get("extra_fields", {}).get("Code", ""))
            lvl_val = p.get("extra_fields", {}).get("LEVEL", p.get("extra_fields", {}).get("Level", ""))
            desc = f"#{p['doc_id']}: {p['unit']} {p['id']}.{p['suffix']} ({code_val} / Lvl {lvl_val})"
            self.cbo_shots.addItem(desc, p)
        if self.points:
            self.cbo_shots.setCurrentIndex(len(self.points) - 1)
        self.cbo_shots.currentIndexChanged.connect(self._update_preview)

        self.txt_site = QLineEdit(self.cfg.point_table or "EXCAVATION")
        self.txt_site.textChanged.connect(self._update_preview)

        fmt_row = QHBoxLayout()
        self.fmt_group = QButtonGroup(self)
        self.rb_ascii = QRadioButton("Plain-Text ASCII (Code-39 *UNIT-ID-SUFFIX*)")
        self.rb_zpl = QRadioButton("Zebra ZPL II (^XA ... ^B3N Barcode)")
        self.rb_ascii.setChecked(True)
        self.fmt_group.addButton(self.rb_ascii)
        self.fmt_group.addButton(self.rb_zpl)
        self.rb_ascii.toggled.connect(self._update_preview)
        fmt_row.addWidget(self.rb_ascii)
        fmt_row.addWidget(self.rb_zpl)

        self.spn_copies = QSpinBox()
        self.spn_copies.setRange(1, 20)
        self.spn_copies.setValue(1)

        spool_row = QHBoxLayout()
        self.txt_spool_path = QLineEdit(str(self.spool_path))
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._browse_spool)
        spool_row.addWidget(self.txt_spool_path, 1)
        spool_row.addWidget(btn_browse)

        form.addRow("Select Recorded Shot:", self.cbo_shots)
        form.addRow("Site / Header Title:", self.txt_site)
        form.addRow("Label Format:", fmt_row)
        form.addRow("Copies to Print:", self.spn_copies)
        form.addRow("Spool File / Port:", spool_row)
        root.addLayout(form)

        prev_box = QGroupBox("Live Label Preview")
        prev_layout = QVBoxLayout(prev_box)
        self.txt_preview = QPlainTextEdit()
        self.txt_preview.setReadOnly(True)
        prev_layout.addWidget(self.txt_preview, 1)
        root.addWidget(prev_box, 1)

        btn_row = QHBoxLayout()
        btn_print = QPushButton("Print / Spool Label(s)")
        btn_print.clicked.connect(self._on_print_clicked)
        btn_view_spool = QPushButton("View Spool File")
        btn_view_spool.clicked.connect(self._on_view_spool)
        btn_clear_spool = QPushButton("Clear Spool File")
        btn_clear_spool.clicked.connect(self.clear_spool_file)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)

        btn_row.addWidget(btn_print)
        btn_row.addWidget(btn_view_spool)
        btn_row.addWidget(btn_clear_spool)
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

        self._update_preview()

    def _browse_spool(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Select Label Spool File", self.txt_spool_path.text(), "Label Spool (*.txt *.zpl *.prn)"
        )
        if path:
            self.txt_spool_path.setText(path)
            self.spool_path = Path(path)

    def current_point_record(self) -> Optional[Dict[str, Any]]:
        data = self.cbo_shots.currentData()
        return data if isinstance(data, dict) else None

    def render_current_label(self) -> str:
        pt = self.current_point_record()
        if not pt:
            return "(No recorded points available to preview)"
        site = self.txt_site.text().strip() or "EXCAVATION"
        if self.rb_zpl.isChecked():
            return format_zpl_bag_label(pt, site_name=site)
        return format_ascii_bag_label(pt, site_name=site)

    def _update_preview(self) -> None:
        self.txt_preview.setPlainText(self.render_current_label())

    def reprint_selected_shot(self) -> str:
        pt = self.current_point_record()
        if not pt:
            raise ValueError("No recorded shots available to print.")
        target_path = Path(self.txt_spool_path.text().strip())
        self.spool_path = target_path
        site = self.txt_site.text().strip() or "EXCAVATION"
        use_zpl = self.rb_zpl.isChecked()
        copies = int(self.spn_copies.value())

        last_payload = ""
        for _ in range(copies):
            last_payload = append_label_to_spool(
                pt, target_path, site_name=site, use_zpl=use_zpl
            )
        return last_payload

    def _on_print_clicked(self) -> None:
        try:
            self.reprint_selected_shot()
            QMessageBox.information(
                self,
                "Label Spooled",
                f"Spooled {self.spn_copies.value()} label copy/copies to:\n{self.spool_path}",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Print Label Error", str(exc))

    def _on_view_spool(self) -> None:
        p = Path(self.txt_spool_path.text().strip())
        if not p.exists():
            self.txt_preview.setPlainText("(Spool file does not exist yet)")
            return
        self.txt_preview.setPlainText(p.read_text(encoding="utf-8"))

    def clear_spool_file(self) -> None:
        p = Path(self.txt_spool_path.text().strip())
        if p.parent.exists():
            p.write_text("", encoding="utf-8")
        self._update_preview()


class HelpViewerDialog(QDialog):
    """
    Replicates frmHelp (0x00438e00 / Help -> Help Topics):
    Provides a searchable offline field manual covering all total-station setups,
    .CFG configuration, SpeedButtons, Context-Dependent Defaults, DataGrid filtering,
    and serial troubleshooting.
    """

    HELP_TOPICS: Dict[str, str] = {
        "1. Overview & Quick Start": (
            "MODERN EDM (EDMWin v3.2 / v3.3.0 Replacement)\n"
            "=============================================\n"
            "Modern EDM is a complete cross-platform replacement for the legacy Visual Basic 6 "
            "EDM Windows program by Harold L. Dibble and Shannon P. McPherron.\n\n"
            "Quick Start Workflow:\n"
            "1. Open an existing .CFG file (File -> Open .CFG) or generate a Paleolithic template "
            "(File -> Create Default CFG and MDB).\n"
            "2. Select your Total Station protocol and COM port (Station & Edit -> Select Total Station & COM Settings).\n"
            "3. Initialize the station coordinates (Station & Edit -> Initialize / Verify Station).\n"
            "4. Press F2 (MEASURE & RECORD SHOT) or click one of the programmable SpeedButtons."
        ),
        "2. Station Initialization & Verification": (
            "INITIALIZING & VERIFYING THE TOTAL STATION\n"
            "==========================================\n"
            "Modern EDM supports 4 station initialization methods plus Station Verify:\n\n"
            "1. Direct / Set Angle: Occupy a known datum nail, enter Instrument Height (HI), "
            "and optionally preset the horizontal circle (Set Horizontal Angle / Flip 180 Degrees).\n"
            "2. 1-Point Setup: Occupy an unknown point with known orientation and shoot 1 known datum.\n"
            "3. 2-Point Backsight: Occupy a known datum and shoot a second backsight datum. Modern EDM "
            "automatically sets the total station's horizontal angle to the computed azimuth and reports "
            "the horizontal distance residual between the two datums.\n"
            "4. Resection (2+ Points): Occupy any free-station point and shoot 2 or more known datums. "
            "Solves (X, Y, Z) and azimuth orientation via least-squares and reports 2D/3D RMS residuals.\n"
            "5. Verify Station: Shoot any known datum to check (dX, dY, dZ) and distance error before excavating."
        ),
        "3. Recording Shots, +Shot Series & Duplicate IDs": (
            "RECORDING SHOTS & MULTI-SHOT (+SHOT) SEQUENCES\n"
            "==============================================\n"
            "- Single Shot [F2]: Records a point with SUFFIX = 0 and automatically increments ID.\n"
            "- Multi-Shot Continuation (+Shot): Check 'Continue Multi-Shot Sequence' to record multiple "
            "points on the same object (e.g. long bone orientation or stratigraphic line). Keeps ID constant "
            "and increments SUFFIX (0, 1, 2...). You cannot change UNIT in the middle of a series.\n"
            "- Duplicate ID Resolution (frmDupID): If (UNIT, ID, SUFFIX) already exists, Modern EDM prompts "
            "you to choose: (1) Convert to alpha ID (e.g. 42a), (2) New ID (max + 1), or (3) Continuation (+Shot).\n"
            "- Deleting Shots in a Series: Deleting an intermediate shot in a multi-shot series automatically "
            "resequences all subsequent SUFFIX numbers in that series."
        ),
        "4. .CFG Fields & Unit-Scoped Carryover": (
            "EDITING .CFG FIELDS & UNIT-SCOPED CARRYOVER\n"
            "===========================================\n"
            "Use 'Station & Edit -> Add/Edit Fields (.CFG)' to define up to 30 fields:\n"
            "- Field Types: MENU (dropdown list), TEXT (alphanumeric), NUMERIC (numbers).\n"
            "- Carry Values to New Shots: Retains the field value after each shot.\n"
            "- Auto-Increment: Increments numeric ID fields after each non-continuation shot.\n"
            "- Unit Field (Unitfields=): Saves and restores field values (such as ID, LEVEL, EXCAVATOR) "
            "independently for each Excavation Unit whenever you switch units."
        ),
        "5. Programmable Shot Buttons ([BUTTON1]-[BUTTON6])": (
            "SPEEDBUTTONS & KEYBOARD SHORTCUTS\n"
            "=================================\n"
            "Use 'Station & Edit -> Shot Buttons ([BUTTON1]-[BUTTON6])' to configure up to 6 macro buttons:\n"
            "- Each button can preset multiple fields at once (e.g. CODE=BONE, LEVEL=2B, PRISM=Mini, UNIT=N100E101) "
            "and immediately trigger a measurement.\n"
            "- Assign a single-key shortcut (e.g. '1' or 'B') to invoke the button via Ctrl+1 or Ctrl+B.\n"
            "- Switching UNIT via a SpeedButton safely restores that unit's next ID without skipping numbers."
        ),
        "6. Context Defaults & 6-Way Coordinate Offsets": (
            "CONTEXT-DEPENDENT DEFAULTS & POST-SHOT OFFSETS\n"
            "==============================================\n"
            "- Context-Dependent Defaults (Station & Edit -> Context Dependent Defaults): Link a Master Variable "
            "(such as LEVEL) to one or more Dependent Variables (such as EXCAVATOR or CODE). Changing the Master "
            "Variable on the main screen automatically fills in the associated Dependent Variable defaults.\n"
            "- 6-Way Offset (Station & Edit -> Offset Last Point): Offset the last recorded point East, West, "
            "North, South, Up, or Down (up to 1000m). Alerts you if the offset moves the point across an "
            "excavation unit boundary."
        ),
        "7. Data Grid, Filtering & GIS Export": (
            "DATA GRID, FILTERING & EXPORTING\n"
            "================================\n"
            "- Show Data Grid (Options -> Show Data Grid): Inspect all recorded shots, toggle visible columns "
            "on the left checklist, or double-click any cell to edit coordinates or attributes inline.\n"
            "- Set Grid Filter (Options -> Set Grid Filter): Filter the grid and plot canvas by any field and "
            "distinct recorded value, or check 'Show only Points from Current Station'.\n"
            "- Exporters (File Menu): Export the 4-table legacy EDMWin CSV bundle (_points, _datums, _prisms, _units) "
            "or a 3D QGIS GeoJSON FeatureCollection (with automatic LineString reconstruction for +Shot series)."
        ),
        "8. Hardware Protocols, Debugging & Bag Labels": (
            "TOTAL STATION PROTOCOLS, DEBUGGING & LABELS\n"
            "===========================================\n"
            "- Supported Instruments: Leica GeoCOM (Wild2), Leica Motorized AutoTilt, Leica/Wild GSI-8 & GSI-16, "
            "Leica Builder, Topcon (with hex & 3-digit decimal BCC verification), Sokkia, Nikon, Microscribe, "
            "Manual Entry, and Offline Simulator.\n"
            "- Debug Total Station (Station & Edit -> Debug Total Station): Send preset GeoCOM commands, run "
            "test X-Shots, or enable serial communication logging via 'Setup Log Files'.\n"
            "- Artifact Bag Labels: Enable 'Options -> Print Shots (Artifact Bag Labels)' or open 'File -> "
            "Print / Reprint Artifact Bag Labels' to preview and spool ASCII or Zebra ZPL II barcode labels."
        ),
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("EDM Help Topics")
        self.resize(820, 520)

        root = QVBoxLayout(self)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search Help:"))
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Filter topics by keyword (e.g. Resection, Button, Offset, GeoCOM)...")
        self.txt_search.textChanged.connect(self.filter_topics)
        search_row.addWidget(self.txt_search, 1)
        root.addLayout(search_row)

        split = QHBoxLayout()
        self.lst_topics = QListWidget()
        self.lst_topics.setMaximumWidth(280)
        self.lst_topics.currentTextChanged.connect(self._on_topic_selected)
        split.addWidget(self.lst_topics)

        self.txt_content = QPlainTextEdit()
        self.txt_content.setReadOnly(True)
        split.addWidget(self.txt_content, 1)
        root.addLayout(split, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        root.addWidget(close_btn)

        self.filter_topics("")

    def filter_topics(self, query: str) -> None:
        q = query.strip().upper()
        self.lst_topics.clear()
        for title, body in self.HELP_TOPICS.items():
            if not q or q in title.upper() or q in body.upper():
                self.lst_topics.addItem(title)
        if self.lst_topics.count() > 0:
            self.lst_topics.setCurrentRow(0)
        else:
            self.txt_content.setPlainText("No matching help topics found.")

    def _on_topic_selected(self, topic_title: str) -> None:
        body = self.HELP_TOPICS.get(topic_title, "")
        self.txt_content.setPlainText(body)


class LegacyMigrationDialog(QDialog):
    """
    Visual Migration Wizard for importing legacy EDMWin projects (.mdb or 4-file CSV bundles)
    into the active Modern EDM SQLite database:
    - Mode 1: 4-File CSV Bundle (<prefix>_points.csv, _datums.csv, _prisms.csv, _units.csv)
    - Mode 2: Legacy Microsoft Access Database (.mdb / .accdb) with optional Points Table hint
    - Displays detailed MigrationReport summary upon completion.
    """

    def __init__(
        self,
        db: EDMDatabase,
        default_dir: Optional[Path] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Legacy EDMWin Project (.MDB / 4-File CSV Bundle)")
        self.resize(640, 440)
        self.db = db
        self.default_dir = default_dir or Path.cwd()
        self.last_report: Optional[Any] = None

        root = QVBoxLayout(self)

        info_lbl = QLabel(
            "Import Datums, Prisms, Excavation Units, and Points from a legacy EDMWin "
            "4-file CSV bundle (_points, _datums, _prisms, _units) or a Microsoft Access (.mdb) "
            "database into the active SQLite project. Unit-scoped IDs and carryover states are "
            "reconstructed automatically."
        )
        info_lbl.setWordWrap(True)
        root.addWidget(info_lbl)

        mode_box = QGroupBox("Migration Source Type")
        mode_row = QHBoxLayout(mode_box)
        self.mode_group = QButtonGroup(self)
        self.rb_csv_bundle = QRadioButton("4-File CSV Bundle (_points, _datums, _prisms, _units)")
        self.rb_mdb = QRadioButton("Microsoft Access Database (.mdb / .accdb)")
        self.rb_csv_bundle.setChecked(True)
        self.mode_group.addButton(self.rb_csv_bundle)
        self.mode_group.addButton(self.rb_mdb)
        mode_row.addWidget(self.rb_csv_bundle)
        mode_row.addWidget(self.rb_mdb)
        root.addWidget(mode_box)

        form = QFormLayout()
        src_row = QHBoxLayout()
        self.txt_source = QLineEdit()
        self.txt_source.setPlaceholderText("Select _points.csv file, bundle prefix, or .mdb file...")
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._on_browse)
        src_row.addWidget(self.txt_source, 1)
        src_row.addWidget(btn_browse)

        self.txt_points_table = QLineEdit()
        self.txt_points_table.setPlaceholderText("Optional: Points table name in .mdb (auto-detected if blank)")

        form.addRow("Source Path / Prefix:", src_row)
        form.addRow("MDB Points Table Hint:", self.txt_points_table)
        root.addLayout(form)

        log_box = QGroupBox("Migration Summary Log")
        log_layout = QVBoxLayout(log_box)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        log_layout.addWidget(self.txt_log, 1)
        root.addWidget(log_box, 1)

        btn_row = QHBoxLayout()
        run_btn = QPushButton("Run Import")
        run_btn.clicked.connect(self._on_run_import)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addStretch()
        btn_row.addWidget(run_btn)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    def _on_browse(self) -> None:
        if self.rb_mdb.isChecked():
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Select Legacy EDMWin .MDB Database",
                str(self.default_dir),
                "Access Databases (*.mdb *.MDB *.accdb)",
            )
        else:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Select Any CSV File in the 4-Table Bundle (e.g. <prefix>_points.csv)",
                str(self.default_dir),
                "CSV Files (*.csv *.CSV)",
            )
        if path:
            self.txt_source.setText(path)

    def run_migration(self, source_path: Optional[str] = None, points_table_hint: str = "") -> Any:
        from storage.legacy_migrator import import_edmwin_csv_bundle, import_edmwin_mdb

        src = (source_path if source_path is not None else self.txt_source.text()).strip()
        if not src:
            raise ValueError("Please select a source CSV bundle or .mdb file to import.")

        hint = (points_table_hint or self.txt_points_table.text()).strip()
        p = Path(src)
        if self.rb_mdb.isChecked() or p.suffix.lower() in (".mdb", ".accdb"):
            rep = import_edmwin_mdb(self.db, p, points_table_hint=hint)
        else:
            rep = import_edmwin_csv_bundle(self.db, p)

        self.last_report = rep
        self.txt_log.setPlainText(rep.summary_text())
        return rep

    def _on_run_import(self) -> None:
        try:
            self.run_migration()
        except Exception as exc:
            QMessageBox.warning(self, "Migration Error", str(exc))


class CloudAndEmailShareDialog(QDialog):
    """
    Unified Cloud Storage (Dropbox & Google Drive) and Email / Gmail Attachment Dialog:
    - Tab 1: Dropbox (Export/Upload & Import/Download via Dropbox API v2 or Local Synced Folder)
    - Tab 2: Google Drive (Export/Upload & Import/Download via Google Drive API v3 or Local Synced Folder)
    - Tab 3: Email & Gmail Share (Attach 4-Table CSV Bundle .zip, Points .csv, GeoJSON, or Full Project .zip
             via Gmail SMTP, Gmail REST API v1, or RFC-5322 .eml Desktop Mail Client Draft)
    Supports 'SIMULATE' tokens/passwords out of the box so all cloud & email flows can be tested offline.
    """

    EXPORT_FORMATS = [
        ("4-Table EDMWin CSV Bundle (.zip)", "CSV_BUNDLE"),
        ("Single Points Table (.csv)", "POINTS_CSV"),
        ("QGIS 3D Survey (.geojson)", "GEOJSON"),
        ("Full Project Backup (.sqlite + CSVs + GeoJSON + .cfg .zip)", "PROJECT_ZIP"),
    ]

    def __init__(
        self,
        db: EDMDatabase,
        cfg: EDMConfig,
        cfg_path: Optional[Path] = None,
        initial_tab: int = 0,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cloud Sync (Dropbox / Google Drive) & Email / Gmail Share")
        self.resize(740, 580)
        self.db = db
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.work_dir = (cfg_path.parent if cfg_path else Path.cwd()) / "_cloud_staging"

        root = QVBoxLayout(self)

        # Shared Export Format selector at top
        fmt_box = QGroupBox("Export / Attachment Package Format")
        fmt_layout = QHBoxLayout(fmt_box)
        fmt_layout.addWidget(QLabel("Package Format:"))
        self.cbo_format = QComboBox()
        for label, code in self.EXPORT_FORMATS:
            self.cbo_format.addItem(label, code)
        fmt_layout.addWidget(self.cbo_format, 1)
        fmt_layout.addWidget(QLabel("Filename Prefix:"))
        self.txt_prefix = QLineEdit(self.cfg.point_table or "excavation_export")
        fmt_layout.addWidget(self.txt_prefix)
        root.addWidget(fmt_box)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_dropbox_tab(), "1. Dropbox Sync")
        self.tabs.addTab(self._build_gdrive_tab(), "2. Google Drive Sync")
        self.tabs.addTab(self._build_email_tab(), "3. Email / Gmail Attachment")
        self.tabs.setCurrentIndex(max(0, min(2, initial_tab)))
        root.addWidget(self.tabs, 1)

        log_box = QGroupBox("Transfer & Activity Log")
        log_layout = QVBoxLayout(log_box)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumHeight(130)
        log_layout.addWidget(self.txt_log)
        root.addWidget(log_box)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        root.addWidget(close_btn)

    def selected_export_format(self) -> str:
        return str(self.cbo_format.currentData() or "CSV_BUNDLE")

    def _build_staged_artifact(self) -> Path:
        from storage.cloud_sync import build_export_artifact

        return build_export_artifact(
            self.db,
            output_dir=self.work_dir,
            export_format=self.selected_export_format(),
            prefix=self.txt_prefix.text().strip() or "excavation_export",
            cfg_path=self.cfg_path,
        )

    # --- Tab 1: Dropbox ---
    def _build_dropbox_tab(self) -> QWidget:
        from storage.cloud_sync import DropboxClient

        w = QWidget()
        v = QVBoxLayout(w)
        form = QFormLayout()

        self.txt_dbx_token = QLineEdit(os.environ.get("DROPBOX_ACCESS_TOKEN", "SIMULATE"))
        self.txt_dbx_token.setPlaceholderText("Dropbox OAuth2 Access Token (or 'SIMULATE' for offline test)")
        self.txt_dbx_remote_path = QLineEdit("/ModernEDM/excavation_export.zip")

        local_sync = DropboxClient.detect_local_sync_folder()
        self.txt_dbx_local_folder = QLineEdit(str(local_sync) if local_sync else "")
        self.txt_dbx_local_folder.setPlaceholderText("Optional local ~/Dropbox folder path")

        form.addRow("Dropbox Access Token:", self.txt_dbx_token)
        form.addRow("Remote Dropbox Path:", self.txt_dbx_remote_path)
        form.addRow("Local Dropbox Folder:", self.txt_dbx_local_folder)
        v.addLayout(form)

        row = QHBoxLayout()
        btn_up = QPushButton("Export && Upload to Dropbox API")
        btn_up.clicked.connect(self.run_dropbox_upload)
        btn_dn = QPushButton("Download && Import from Dropbox API")
        btn_dn.clicked.connect(self.run_dropbox_download)
        btn_local = QPushButton("Copy Export to Local Dropbox Folder")
        btn_local.clicked.connect(self._on_copy_to_local_dropbox)
        row.addWidget(btn_up)
        row.addWidget(btn_dn)
        row.addWidget(btn_local)
        v.addLayout(row)
        v.addStretch()
        return w

    def run_dropbox_upload(self) -> Any:
        from storage.cloud_sync import DropboxClient

        artifact = self._build_staged_artifact()
        remote_p = self.txt_dbx_remote_path.text().strip() or f"/ModernEDM/{artifact.name}"
        if not remote_p.endswith(artifact.suffix):
            remote_p = f"{remote_p.rstrip('/')}/{artifact.name}"
        client = DropboxClient(self.txt_dbx_token.text())
        res = client.upload_file(artifact, remote_p)
        self.txt_log.appendPlainText(res.message)
        return res

    def run_dropbox_download(self, simulated_bytes: Optional[bytes] = None) -> Dict[str, int]:
        from storage.cloud_sync import DropboxClient, import_artifact_into_db

        remote_p = self.txt_dbx_remote_path.text().strip() or "/ModernEDM/excavation_export.zip"
        local_dst = self.work_dir / Path(remote_p).name
        client = DropboxClient(self.txt_dbx_token.text())
        res = client.download_file(remote_p, local_dst, simulated_source_bytes=simulated_bytes)
        counts = import_artifact_into_db(self.db, local_dst, extract_dir=self.work_dir / "dbx_extract")
        self.txt_log.appendPlainText(f"{res.message}\nImported into SQLite: {counts}")
        return counts

    def _on_copy_to_local_dropbox(self) -> None:
        folder = self.txt_dbx_local_folder.text().strip()
        if not folder:
            path = QFileDialog.getExistingDirectory(self, "Select Local Dropbox Folder")
            if not path:
                return
            folder = path
            self.txt_dbx_local_folder.setText(folder)
        artifact = self._build_staged_artifact()
        dest = Path(folder) / artifact.name
        Path(folder).mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact, dest)
        self.txt_log.appendPlainText(f"Copied {artifact.name} to local Dropbox folder: {dest}")

    # --- Tab 2: Google Drive ---
    def _build_gdrive_tab(self) -> QWidget:
        from storage.cloud_sync import GoogleDriveClient

        w = QWidget()
        v = QVBoxLayout(w)
        form = QFormLayout()

        self.txt_gdrive_token = QLineEdit(os.environ.get("GOOGLE_DRIVE_TOKEN", "SIMULATE"))
        self.txt_gdrive_token.setPlaceholderText("Google OAuth2 Bearer Token (or 'SIMULATE' for offline test)")
        self.txt_gdrive_folder_id = QLineEdit("")
        self.txt_gdrive_folder_id.setPlaceholderText("Optional Drive Folder ID (leave blank for My Drive root)")
        self.txt_gdrive_file_id = QLineEdit("gdrive_sim_excavation_export.zip")
        self.txt_gdrive_file_id.setPlaceholderText("Google Drive File ID for Download/Import")

        local_sync = GoogleDriveClient.detect_local_sync_folder()
        self.txt_gdrive_local_folder = QLineEdit(str(local_sync) if local_sync else "")
        self.txt_gdrive_local_folder.setPlaceholderText("Optional local Google Drive folder path")

        form.addRow("Google OAuth2 Token:", self.txt_gdrive_token)
        form.addRow("Target Drive Folder ID:", self.txt_gdrive_folder_id)
        form.addRow("Drive File ID (for Import):", self.txt_gdrive_file_id)
        form.addRow("Local Google Drive Folder:", self.txt_gdrive_local_folder)
        v.addLayout(form)

        row = QHBoxLayout()
        btn_up = QPushButton("Export && Upload to Google Drive API")
        btn_up.clicked.connect(self.run_gdrive_upload)
        btn_dn = QPushButton("Download && Import from Google Drive API")
        btn_dn.clicked.connect(self.run_gdrive_download)
        btn_local = QPushButton("Copy Export to Local Google Drive")
        btn_local.clicked.connect(self._on_copy_to_local_gdrive)
        row.addWidget(btn_up)
        row.addWidget(btn_dn)
        row.addWidget(btn_local)
        v.addLayout(row)
        v.addStretch()
        return w

    def run_gdrive_upload(self) -> Any:
        from storage.cloud_sync import GoogleDriveClient

        artifact = self._build_staged_artifact()
        client = GoogleDriveClient(self.txt_gdrive_token.text())
        res = client.upload_file(
            artifact,
            drive_filename=artifact.name,
            folder_id=self.txt_gdrive_folder_id.text(),
        )
        self.txt_gdrive_file_id.setText(res.remote_id_or_path)
        self.txt_log.appendPlainText(res.message)
        return res

    def run_gdrive_download(self, simulated_bytes: Optional[bytes] = None) -> Dict[str, int]:
        from storage.cloud_sync import GoogleDriveClient, import_artifact_into_db

        fid = self.txt_gdrive_file_id.text().strip() or "gdrive_sim_excavation_export.zip"
        ext = ".zip" if not fid.lower().endswith((".csv", ".zip", ".mdb")) else ""
        local_dst = self.work_dir / f"{Path(fid).name}{ext}"
        client = GoogleDriveClient(self.txt_gdrive_token.text())
        res = client.download_file(fid, local_dst, simulated_source_bytes=simulated_bytes)
        counts = import_artifact_into_db(self.db, local_dst, extract_dir=self.work_dir / "gdrive_extract")
        self.txt_log.appendPlainText(f"{res.message}\nImported into SQLite: {counts}")
        return counts

    def _on_copy_to_local_gdrive(self) -> None:
        folder = self.txt_gdrive_local_folder.text().strip()
        if not folder:
            path = QFileDialog.getExistingDirectory(self, "Select Local Google Drive Folder")
            if not path:
                return
            folder = path
            self.txt_gdrive_local_folder.setText(folder)
        artifact = self._build_staged_artifact()
        dest = Path(folder) / artifact.name
        Path(folder).mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact, dest)
        self.txt_log.appendPlainText(f"Copied {artifact.name} to local Google Drive folder: {dest}")

    # --- Tab 3: Email & Gmail Attachment ---
    def _build_email_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        form = QFormLayout()

        self.txt_smtp_host = QLineEdit("smtp.gmail.com")
        self.spn_smtp_port = QSpinBox()
        self.spn_smtp_port.setRange(1, 65535)
        self.spn_smtp_port.setValue(587)

        self.txt_sender = QLineEdit(os.environ.get("EDM_SENDER_EMAIL", "excavation@gmail.com"))
        self.txt_password = QLineEdit(os.environ.get("EDM_EMAIL_PASSWORD", "SIMULATE"))
        self.txt_password.setEchoMode(QLineEdit.Password)
        self.txt_password.setPlaceholderText("Gmail App Password, OAuth2 Token, or 'SIMULATE'")

        self.txt_recipients = QLineEdit("lab-director@example.edu")
        self.txt_subject = QLineEdit(f"Modern EDM Survey Export — {self.cfg.point_table or 'Site'}")
        self.txt_body = QPlainTextEdit(
            "Attached is the latest total-station excavation export from Modern EDM."
        )
        self.txt_body.setMaximumHeight(75)

        host_row = QHBoxLayout()
        host_row.addWidget(self.txt_smtp_host, 1)
        host_row.addWidget(QLabel("Port:"))
        host_row.addWidget(self.spn_smtp_port)

        form.addRow("SMTP Server (Gmail default):", host_row)
        form.addRow("From (Sender Email):", self.txt_sender)
        form.addRow("App Password / OAuth Token:", self.txt_password)
        form.addRow("To (Comma-separated):", self.txt_recipients)
        form.addRow("Subject:", self.txt_subject)
        form.addRow("Message Body:", self.txt_body)
        v.addLayout(form)

        row = QHBoxLayout()
        btn_smtp = QPushButton("Send via Gmail / SMTP")
        btn_smtp.clicked.connect(self.run_send_smtp)
        btn_gmail_api = QPushButton("Send via Gmail REST API")
        btn_gmail_api.clicked.connect(self.run_send_gmail_api)
        btn_eml = QPushButton("Save .EML Draft (Open in Mail App)")
        btn_eml.clicked.connect(self.run_save_eml_draft)
        row.addWidget(btn_smtp)
        row.addWidget(btn_gmail_api)
        row.addWidget(btn_eml)
        v.addLayout(row)
        return w

    def run_send_smtp(self) -> Any:
        from storage.cloud_sync import EmailShareClient

        artifact = self._build_staged_artifact()
        res = EmailShareClient.send_via_smtp(
            smtp_host=self.txt_smtp_host.text(),
            smtp_port=int(self.spn_smtp_port.value()),
            sender_email=self.txt_sender.text(),
            password_or_app_password=self.txt_password.text(),
            recipient_emails=self.txt_recipients.text(),
            subject=self.txt_subject.text(),
            body_text=self.txt_body.toPlainText(),
            attachment_paths=[artifact],
        )
        self.txt_log.appendPlainText(res.message)
        return res

    def run_send_gmail_api(self) -> Any:
        from storage.cloud_sync import EmailShareClient

        artifact = self._build_staged_artifact()
        res = EmailShareClient.send_via_gmail_api(
            oauth_access_token=self.txt_password.text(),
            sender_email=self.txt_sender.text(),
            recipient_emails=self.txt_recipients.text(),
            subject=self.txt_subject.text(),
            body_text=self.txt_body.toPlainText(),
            attachment_paths=[artifact],
        )
        self.txt_log.appendPlainText(res.message)
        return res

    def run_save_eml_draft(self, dest_eml_path: Optional[Path] = None) -> Any:
        from storage.cloud_sync import EmailShareClient

        artifact = self._build_staged_artifact()
        eml_path = dest_eml_path or (self.work_dir / f"{artifact.stem}.eml")
        res = EmailShareClient.save_eml_draft(
            eml_dest_path=eml_path,
            sender_email=self.txt_sender.text(),
            recipient_emails=self.txt_recipients.text(),
            subject=self.txt_subject.text(),
            body_text=self.txt_body.toPlainText(),
            attachment_paths=[artifact],
        )
        self.txt_log.appendPlainText(res.message)
        return res


