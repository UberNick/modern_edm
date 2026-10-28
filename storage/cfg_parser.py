"""
Legacy EDMWin .CFG Parser, [BUTTON1]-[BUTTON6] Macro Engine & Configuration Serializer.
Includes full parity with decompiled edmwin.exe (FUN_004b9ed0 OpenCFG & FUN_004b7bb0 save_cfgfile):
- Case-insensitive INI parsing ([EDM], [General], [BUTTON1..6], [Field1..N], or named field sections)
- Multi-field [BUTTON1]-[BUTTON6] macros (TITLE, Shortcut, Alpha/Numeric ID mode, arbitrary field presets)
- Unit-scoped fields (UnitField=True / Unitfields=: ID, LEVEL, EXCAVATOR state saved/restored per Excavation Unit)
- Auto-populated system fields (DatumName, DatumX, DatumY, DatumZ, Prism, DateTime)
- Multi-shot continuation (Keep ID, increment SUFFIX)
- SpeedButtons (including safe handling when UNIT is in SpeedButtons without skipping IDs)
- Legacy .CFG atomic write-back via $temp$.ini swap (FUN_004b7bb0)
"""

from __future__ import annotations

import configparser
import datetime
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


SYSTEM_AUTO_FIELDS = {
    "X", "Y", "Z", "DATUMNAME", "DATUMX", "DATUMY", "DATUMZ",
    "PRISM", "SLOPEDIST", "SLOPED", "HANGLE", "VANGLE"
}


@dataclass
class FieldSpec:
    name: str
    field_type: str = "TEXT"       # TEXT, MENU, INTEGER, NUMERIC, BOOLEAN, DATETIME, NOTE, INSTRUMENT
    length: int = 32
    prompt: str = ""
    menu_items: List[str] = field(default_factory=list)
    carry: bool = False
    increment: bool = False
    unit_field: bool = False       # Saved/restored per unique UNIT in the Units table
    required: bool = False
    unique_check: bool = False
    case_mode: str = "NONE"        # NONE, UPPER, LOWER
    default: str = ""


@dataclass
class SpeedButtonMacro:
    """
    Represents a [BUTTON1]..[BUTTON6] block in edmwin.exe (FUN_0045fc90 & FUN_004b7bb0).
    Can preset multiple fields at once (e.g. CODE=BONE, LEVEL=2A, UNIT=N100E101, PRISM=Mini).
    """
    index: int
    title: str
    shortcut: str = ""             # e.g. '1', 'B' (invoked as Ctrl+<shortcut>)
    id_type: str = "NUMERIC"       # 'NUMERIC' or 'ALPHA'
    field_values: Dict[str, str] = field(default_factory=dict)


@dataclass
class EDMConfig:
    station_type: str = "Simulator"
    com_port: str = "COM1"
    baudrate: int = 19200
    parity: str = "N"
    bytesize: int = 8
    stopbits: int = 1
    edm_delay_time: float = 0.5
    prompt_prism: bool = False
    print_labels: bool = False
    auto_find_unit: bool = False
    speech_enabled: bool = False
    limit_checking: bool = True
    update_alerts: bool = True
    upper_case: bool = False
    database_name: str = ""
    db_path: str = ""
    point_table: str = "Points"
    station_name: str = "STN1"
    station_x: Optional[float] = None
    station_y: Optional[float] = None
    station_z: Optional[float] = None
    nikon_prism_offset_mm: float = 17.0
    unit_fields_Explicit: List[str] = field(default_factory=list)
    speed_buttons: List[str] = field(default_factory=list)
    button_macros: List[SpeedButtonMacro] = field(default_factory=list)
    fields: List[FieldSpec] = field(default_factory=list)

    def get_unit_scoped_field_names(self) -> List[str]:
        """Returns field names that are scoped per Excavation Unit (e.g., ID, LEVEL, EXCAVATOR)."""
        names: List[str] = list(self.unit_fields_Explicit)
        for f in self.fields:
            if f.unit_field and f.name not in names:
                names.append(f.name)
            elif f.name.upper() in ("ID", "LEVEL", "EXCAVATOR") and f.name not in names:
                if f.increment or f.carry:
                    names.append(f.name)
        return names

    def format_field_value(self, field_spec: FieldSpec, raw_val: Any) -> str:
        if field_spec.field_type == "DATETIME" and not str(raw_val).strip():
            return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        s = str(raw_val if raw_val is not None else field_spec.default).strip()
        if self.upper_case or field_spec.case_mode == "UPPER":
            s = s.upper()
        elif field_spec.case_mode == "LOWER":
            s = s.lower()
        if field_spec.length > 0 and len(s) > field_spec.length:
            s = s[: field_spec.length]
        return s

    def apply_button_macro(
        self,
        macro: SpeedButtonMacro,
        current_values: Dict[str, Any],
        unit_state_lookup_fn: Optional[ Any ] = None,
    ) -> Dict[str, Any]:
        """
        Replicates FUN_0045fc90 (Button_Click):
        Applies all field presets in a [BUTTONn] macro to current_values.
        Includes the June 12, 2023 bug fix: if the button macro changes UNIT,
        restores that unit's current ID/unit-scoped fields via unit_state_lookup_fn
        without double-incrementing ID.
        """
        updated = dict(current_values)
        old_unit = str(updated.get("UNIT", "")).strip()

        # Check if macro overrides UNIT (case-insensitive key match)
        new_unit: Optional[str] = None
        for k, v in macro.field_values.items():
            if k.upper() == "UNIT":
                new_unit = v.strip()
                break

        if new_unit and new_unit.upper() != old_unit.upper() and callable(unit_state_lookup_fn):
            unit_saved = unit_state_lookup_fn(new_unit)
            if isinstance(unit_saved, dict):
                for uk, uv in unit_saved.items():
                    updated[uk] = str(uv)

        # Apply all field assignments from the macro
        for k, v in macro.field_values.items():
            matched_key = k
            for f in self.fields:
                if f.name.upper() == k.upper():
                    matched_key = f.name
                    break
            updated[matched_key] = v

        return updated

    def apply_post_record_rules(
        self,
        current_values: Dict[str, Any],
        continue_multi_shot_sequence: bool = False,
        unit_state_lookup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Computes the next form field values after recording a shot:
        - If continue_multi_shot_sequence is True:
          ID does NOT increment, and SUFFIX increments by 1 (0 -> 1 -> 2...).
        - If continue_multi_shot_sequence is False:
          Fields with Increment=True increment by 1, SUFFIX resets to 0,
          Carry=True fields keep their values, Carry=False fields reset to Default.
        - If unit_state_lookup is provided (when switching units), overrides unit-scoped fields.
        """
        next_values: Dict[str, Any] = {}
        for f in self.fields:
            fname_upper = f.name.upper()
            if fname_upper in SYSTEM_AUTO_FIELDS:
                continue
            raw_val = current_values.get(f.name, f.default)

            if continue_multi_shot_sequence:
                if fname_upper == "SUFFIX":
                    try:
                        next_values[f.name] = str(int(raw_val) + 1)
                    except (ValueError, TypeError):
                        next_values[f.name] = "1"
                else:
                    next_values[f.name] = self.format_field_value(f, raw_val)
            else:
                if fname_upper == "SUFFIX":
                    next_values[f.name] = f.default or "0"
                elif f.increment:
                    try:
                        next_values[f.name] = str(int(raw_val) + 1)
                    except (ValueError, TypeError):
                        next_values[f.name] = "1"
                elif f.carry:
                    next_values[f.name] = self.format_field_value(f, raw_val)
                elif f.field_type == "DATETIME":
                    next_values[f.name] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                else:
                    next_values[f.name] = f.default

            if unit_state_lookup and f.name in unit_state_lookup:
                next_values[f.name] = str(unit_state_lookup[f.name])

        return next_values


def _parse_bool(val: str, default: bool = False) -> bool:
    v = val.strip().lower()
    if v in ("1", "true", "yes", "y", "on", "-1"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return default


def parse_cfg_text(text: str) -> EDMConfig:
    """
    Parses raw EDMWin .CFG text content into an EDMConfig instance.
    Supports [EDM], [BUTTON1]..[BUTTON6], and field sections.
    """
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # preserve original key casing for [BUTTONn] field names
    parser.read_string(text)

    cfg = EDMConfig()
    for section in parser.sections():
        sec_clean = section.strip()
        sec_upper = sec_clean.upper()
        raw_items = dict(parser.items(section))
        items = {k.strip().lower(): v.strip() for k, v in raw_items.items()}

        if sec_upper in ("EDM", "GENERAL", "STATION", "OPTIONS"):
            if "stationtype" in items:
                cfg.station_type = items["stationtype"]
            elif "instrument" in items:
                cfg.station_type = items["instrument"]
            elif "station" in items:
                cfg.station_type = items["station"]

            if "comport" in items:
                port_val = items["comport"]
                cfg.com_port = f"COM{port_val}" if port_val.isdigit() else port_val

            if "settings" in items:
                parts = [p.strip() for p in items["settings"].split(",")]
                if len(parts) >= 4:
                    cfg.baudrate = int(parts[0])
                    cfg.parity = parts[1].upper()[0]
                    cfg.bytesize = int(parts[2])
                    cfg.stopbits = int(parts[3])

            if "edmdelaytime" in items:
                try:
                    cfg.edm_delay_time = float(items["edmdelaytime"])
                except ValueError:
                    pass

            if "promptprism" in items or "prismprompt" in items:
                cfg.prompt_prism = _parse_bool(items.get("promptprism", items.get("prismprompt", "no")))

            if "printlabels" in items or "autoprint" in items:
                cfg.print_labels = _parse_bool(items.get("printlabels", items.get("autoprint", "no")))

            if "limitchecking" in items:
                cfg.limit_checking = _parse_bool(items["limitchecking"], True)

            if "updatealerts" in items:
                cfg.update_alerts = _parse_bool(items["updatealerts"], True)

            if "uppercase" in items:
                cfg.upper_case = _parse_bool(items["uppercase"], False)

            if "database" in items:
                cfg.database_name = items["database"]
            if "dbpath" in items:
                cfg.db_path = items["dbpath"]
            if "pointtable" in items:
                cfg.point_table = items["pointtable"]

            if "stationname" in items:
                cfg.station_name = items["stationname"]
            if "stationx" in items:
                try:
                    cfg.station_x = float(items["stationx"])
                except ValueError:
                    pass
            if "stationy" in items:
                try:
                    cfg.station_y = float(items["stationy"])
                except ValueError:
                    pass
            if "stationz" in items:
                try:
                    cfg.station_z = float(items["stationz"])
                except ValueError:
                    pass

            if "unitfields" in items and items["unitfields"]:
                cfg.unit_fields_Explicit = [
                    u.strip() for u in items["unitfields"].split(",") if u.strip()
                ]

            if "nikonprismoffset" in items:
                try:
                    cfg.nikon_prism_offset_mm = float(items["nikonprismoffset"])
                except ValueError:
                    pass

            if "speedbuttons" in items:
                cfg.speed_buttons = [
                    b.strip() for b in items["speedbuttons"].split(",") if b.strip()
                ]
                for idx, sb in enumerate(cfg.speed_buttons, start=1):
                    cfg.button_macros.append(
                        SpeedButtonMacro(index=idx, title=sb, field_values={"CODE": sb})
                    )

        elif sec_upper.startswith("BUTTON") and sec_upper[6:].isdigit():
            btn_idx = int(sec_upper[6:])
            title = items.get("title", f"Button{btn_idx}")
            shortcut = items.get("shortcut", "")
            id_type = "ALPHA" if _parse_bool(items.get("alpha", "false")) else "NUMERIC"
            field_vals: Dict[str, str] = {}
            for orig_k, orig_v in raw_items.items():
                kl = orig_k.strip().lower()
                if kl not in ("title", "shortcut", "alpha", "numeric", "idtype"):
                    field_vals[orig_k.strip()] = orig_v.strip()

            macro = SpeedButtonMacro(
                index=btn_idx,
                title=title,
                shortcut=shortcut,
                id_type=id_type,
                field_values=field_vals,
            )
            cfg.button_macros.append(macro)
            if title not in cfg.speed_buttons:
                cfg.speed_buttons.append(title)

        else:
            field_name = items.get("name", sec_clean)
            if field_name.upper() in SYSTEM_AUTO_FIELDS:
                continue

            raw_type = items.get("type", "").strip().upper()
            menu_raw = items.get("menu", "")
            menu_items = [m.strip() for m in menu_raw.split(",") if m.strip()]

            if not raw_type:
                ftype = "MENU" if menu_items else "TEXT"
            elif raw_type == "INSTRUMENT":
                ftype = "NUMERIC"
            elif raw_type == "BOOLEAN":
                ftype = "BOOLEAN"
                if not menu_items:
                    menu_items = ["True", "False"]
            elif menu_items and raw_type == "TEXT":
                ftype = "MENU"
            else:
                ftype = raw_type

            length = int(items.get("length", "32")) if items.get("length", "").isdigit() else 32
            prompt = items.get("prompt", field_name)
            carry = _parse_bool(items.get("carry", "false"))
            increment = _parse_bool(items.get("increment", "false"))
            is_in_explicit_unit = any(u.upper() == field_name.upper() for u in cfg.unit_fields_Explicit)
            unit_field = is_in_explicit_unit or _parse_bool(
                items.get(
                    "unitfield",
                    items.get("unit", "true" if field_name.upper() in ("ID", "LEVEL", "EXCAVATOR") else "false"),
                )
            )
            required = _parse_bool(items.get("required", "false"))
            unique_check = _parse_bool(items.get("unique", "false"))
            case_mode = items.get("case", "NONE").strip().upper()
            default_val = items.get("default", menu_items[0] if menu_items and carry else "")

            cfg.fields.append(
                FieldSpec(
                    name=field_name,
                    field_type=ftype,
                    length=length,
                    prompt=prompt,
                    menu_items=menu_items,
                    carry=carry,
                    increment=increment,
                    unit_field=unit_field,
                    required=required,
                    unique_check=unique_check,
                    case_mode=case_mode,
                    default=default_val,
                )
            )

    return cfg


def save_legacy_cfg_file(cfg: EDMConfig, path: str | Path) -> None:
    """
    Replicates FUN_004b7bb0 (save_cfgfile in edmwin.exe):
    Writes [EDM], [BUTTON1]..[BUTTON6], and [<FieldName>] blocks via an atomic
    '$temp$.ini' swap so an interrupted write never corrupts the active .CFG.
    """
    target = Path(path)
    temp_path = target.parent / "$temp$.ini"

    lines: List[str] = ["[EDM]"]
    if cfg.database_name:
        lines.append(f"Database={cfg.database_name}")
    if cfg.db_path:
        lines.append(f"DBPath={cfg.db_path}")
    lines.append(f"PointTable={cfg.point_table}")
    unit_fields = cfg.get_unit_scoped_field_names()
    lines.append(f"Unitfields={','.join(unit_fields)}")
    lines.append(f"Limitchecking={'Yes' if cfg.limit_checking else 'No'}")
    lines.append(f"UpdateAlerts={'YES' if cfg.update_alerts else 'No'}")
    lines.append(f"PrismPrompt={'YES' if cfg.prompt_prism else 'NO'}")
    lines.append(f"Instrument={cfg.station_type}")
    lines.append(f"COMport={cfg.com_port}")
    lines.append(f"Settings={cfg.baudrate},{cfg.parity},{cfg.bytesize},{cfg.stopbits}")
    lines.append(f"EDMDelayTime={cfg.edm_delay_time}")
    lines.append(f"StationName={cfg.station_name}")
    if cfg.station_x is not None:
        lines.append(f"StationX={cfg.station_x:.3f}")
    if cfg.station_y is not None:
        lines.append(f"stationY={cfg.station_y:.3f}")
    if cfg.station_z is not None:
        lines.append(f"stationZ={cfg.station_z:.3f}")
    lines.append("")

    for macro in cfg.button_macros:
        lines.append(f"[BUTTON{macro.index}]")
        lines.append(f"TITLE={macro.title}")
        if macro.shortcut:
            lines.append(f"Shortcut={macro.shortcut}")
        for fk, fv in macro.field_values.items():
            lines.append(f"{fk}={fv}")
        lines.append("")

    for f in cfg.fields:
        lines.append(f"[{f.name}]")
        lines.append(f"Prompt={f.prompt or f.name}")
        lines.append(f"Length={f.length}")
        lines.append(f"Type={f.field_type}")
        if f.menu_items:
            lines.append(f"Menu={','.join(f.menu_items)}")
        if f.carry:
            lines.append("Carry=True")
        if f.increment:
            lines.append("Increment=True")
        lines.append("")

    temp_path.write_text("\n".join(lines), encoding="utf-8")
    temp_path.replace(target)


def load_cfg_file(path: str | Path) -> EDMConfig:
    p = Path(path)
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        fields = [FieldSpec(**f) for f in data.pop("fields", [])]
        macros = [SpeedButtonMacro(**m) for m in data.pop("button_macros", [])]
        return EDMConfig(**data, fields=fields, button_macros=macros)
    try:
        raw_text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw_text = p.read_text(encoding="latin-1")
    return parse_cfg_text(raw_text)


def save_cfg_json(cfg: EDMConfig, path: str | Path) -> None:
    p = Path(path)
    p.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
