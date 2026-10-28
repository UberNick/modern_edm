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
from typing import Dict, List, Optional, Tuple

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
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

from drivers.total_station import SerialConfig, TotalStationDriver, create_driver
from storage.cfg_parser import EDMConfig, FieldSpec, SpeedButtonMacro
from storage.database import EDMDatabase
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
    def __init__(
        self,
        station_type: str,
        serial_config: SerialConfig,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("COM / Bluetooth Settings & Serial Diagnostic Terminal")
        self.resize(680, 480)
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
            "Microscribe",
        ])
        self.station_combo.setCurrentText(self.station_type)

        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self._refresh_ports()
        self.port_combo.setCurrentText(self.serial_config.port)

        scan_btn = QPushButton("Look for Ports")
        scan_btn.clicked.connect(self._refresh_ports)
        port_row = QHBoxLayout()
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(scan_btn)

        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"])
        self.baud_combo.setCurrentText(str(self.serial_config.baudrate))

        self.parity_combo = QComboBox()
        self.parity_combo.addItems(["N", "E", "O"])
        self.parity_combo.setCurrentText(self.serial_config.parity)

        form.addRow("Station Protocol:", self.station_combo)
        form.addRow("Serial / BT Port:", port_row)
        form.addRow("Baud Rate:", self.baud_combo)
        form.addRow("Parity:", self.parity_combo)
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

    def _refresh_ports(self) -> None:
        self.port_combo.clear()
        for dev, desc in TotalStationDriver.list_available_ports():
            self.port_combo.addItem(desc, dev)

    def _append_log(self, direction: str, text: str) -> None:
        self.log_view.appendPlainText(f"[{direction}] {text}")

    def build_config(self) -> Tuple[str, SerialConfig]:
        port_data = self.port_combo.currentData()
        port_str = port_data if port_data else self.port_combo.currentText().split(" - ")[0].strip()
        cfg = SerialConfig(
            port=port_str,
            baudrate=int(self.baud_combo.currentText()),
            parity=self.parity_combo.currentText(),
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
