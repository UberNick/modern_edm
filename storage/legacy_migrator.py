"""
Legacy EDMWin (.mdb / 4-File CSV Bundle) to Modern EDM SQLite Migration Utility.
Replicates all table schemas and column conventions from decompiled edmwin.exe (v3.05 / v3.2):
- Imports 4-table CSV bundles (<prefix>_points.csv, <prefix>_datums.csv, <prefix>_prisms.csv, <prefix>_units.csv)
- Imports Microsoft Jet / Access .mdb databases via:
  1. pyodbc (Microsoft Access Driver (*.mdb, *.accdb) on Windows)
  2. mdbtools (`mdb-tables` / `mdb-export` CLI on macOS / Linux / Windows)
  3. Built-in Windows PowerShell 32-bit/64-bit OLEDB/ADO Jet fallback (zero third-party Python packages required!)
- Automatically reconstructs Unit-Scoped Carryover State (`unit_field_state`) from the imported records.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from storage.database import EDMDatabase
from survey.math_engine import Datum, ExcavationUnit, Prism, StationState


@dataclass
class MigrationReport:
    source_type: str
    source_path: str
    points_imported: int = 0
    datums_imported: int = 0
    prisms_imported: int = 0
    units_imported: int = 0
    units_state_rebuilt: int = 0

    def summary_text(self) -> str:
        return (
            f"Source ({self.source_type}): {self.source_path}\n"
            f"  - Datums imported / updated : {self.datums_imported}\n"
            f"  - Prisms imported / updated : {self.prisms_imported}\n"
            f"  - Units imported / updated  : {self.units_imported}\n"
            f"  - Points imported           : {self.points_imported}\n"
            f"  - Unit states reconstructed : {self.units_state_rebuilt}"
        )


def _ci_get(row: Dict[str, Any], *candidates: str, default: Any = "") -> Any:
    """Case-insensitive dictionary lookup across multiple candidate column names."""
    upper_map = {str(k).strip().upper(): v for k, v in row.items() if k is not None}
    for cand in candidates:
        key = cand.strip().upper()
        if key in upper_map and upper_map[key] is not None:
            val = str(upper_map[key]).strip()
            if val != "":
                return val
    return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        s = str(val).strip()
        return float(s) if s != "" else default
    except (ValueError, TypeError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        s = str(val).strip()
        return int(float(s)) if s != "" else default
    except (ValueError, TypeError):
        return default


def import_datums_rows(db: EDMDatabase, rows: Iterable[Dict[str, Any]]) -> int:
    """
    Imports datum records from either modern CSV headers (NAME, X, Y, Z, DESCRIPTION)
    or legacy EDMWin EDM_Datums / Datums table columns.
    """
    count = 0
    for row in rows:
        name = str(_ci_get(row, "NAME", "DATUMNAME", "DATUM", default="")).strip()
        if not name:
            continue
        x = _safe_float(_ci_get(row, "X", "DATUMX", default=0.0))
        y = _safe_float(_ci_get(row, "Y", "DATUMY", default=0.0))
        z = _safe_float(_ci_get(row, "Z", "DATUMZ", default=0.0))
        desc = str(_ci_get(row, "DESCRIPTION", "DESC", "NOTES", "COMMENT", default=""))
        db.upsert_datum(Datum(name=name, x=x, y=y, z=z, description=desc))
        count += 1
    return count


def import_prisms_rows(db: EDMDatabase, rows: Iterable[Dict[str, Any]]) -> int:
    """
    Imports prism records from either modern CSV headers (NAME, HEIGHT, OFFSET, DESCRIPTION)
    or legacy EDMWin EDM_Poles / Prisms table columns.
    """
    count = 0
    for row in rows:
        name = str(_ci_get(row, "NAME", "PRISMNAME", "PRISM", "POLE", default="")).strip()
        if not name:
            continue
        height = _safe_float(_ci_get(row, "HEIGHT", "POLEHEIGHT", "PRISM_HT", "HT", default=0.0))
        offset = _safe_float(_ci_get(row, "OFFSET", "PRISMOFFSET", "CONST", default=0.0))
        desc = str(_ci_get(row, "DESCRIPTION", "DESC", "NOTES", default=""))
        db.upsert_prism(Prism(name=name[:15], height=height, offset=offset, description=desc))
        count += 1
    return count


def import_units_rows(db: EDMDatabase, rows: Iterable[Dict[str, Any]]) -> Tuple[int, int]:
    """
    Imports excavation unit boundaries and optional unit-scoped carryover state from either:
    - Modern CSV headers: NAME, SHAPE, MIN_X, MIN_Y, MAX_X, MAX_Y, CENTER_X, CENTER_Y, RADIUS
    - Legacy EDMWin EDM_Units table: Unit / Name, X1 / MinX, X2 / MaxX, Y1 / MinY, Y2 / MaxY,
      CenterX, CenterY, Radius, plus any unit-scoped carryover columns (ID, LEVEL, EXCAVATOR).
    Returns (units_imported, unit_states_imported).
    """
    reserved_unit_cols = {
        "NAME", "UNIT", "UNITNAME", "SHAPE",
        "MIN_X", "MINX", "X1", "LEFTX",
        "MIN_Y", "MINY", "Y1", "LOWERY",
        "MAX_X", "MAXX", "X2", "RIGHTX",
        "MAX_Y", "MAXY", "Y2", "UPPERY",
        "CENTER_X", "CENTERX", "CX",
        "CENTER_Y", "CENTERY", "CY",
        "RADIUS", "RAD", "R",
        "MIN_Z", "MINZ", "Z1",
        "MAX_Z", "MAXZ", "Z2",
    }
    u_count = 0
    state_count = 0
    for row in rows:
        name = str(_ci_get(row, "NAME", "UNIT", "UNITNAME", default="")).strip()
        if not name:
            continue
        shape_raw = str(_ci_get(row, "SHAPE", default="")).strip().upper()
        min_x = _safe_float(_ci_get(row, "MIN_X", "MINX", "X1", default=0.0))
        min_y = _safe_float(_ci_get(row, "MIN_Y", "MINY", "Y1", default=0.0))
        max_x = _safe_float(_ci_get(row, "MAX_X", "MAXX", "X2", default=0.0))
        max_y = _safe_float(_ci_get(row, "MAX_Y", "MAXY", "Y2", default=0.0))
        center_x = _safe_float(_ci_get(row, "CENTER_X", "CENTERX", "CX", default=0.0))
        center_y = _safe_float(_ci_get(row, "CENTER_Y", "CENTERY", "CY", default=0.0))
        radius = _safe_float(_ci_get(row, "RADIUS", "RAD", "R", default=0.0))

        if not shape_raw:
            if radius > 0.0:
                shape_raw = "CIRCLE"
            elif min_x == 0.0 and max_x == 0.0 and min_y == 0.0 and max_y == 0.0:
                shape_raw = "NOLIMITS"
            else:
                shape_raw = "RECT"

        if min_x > max_x:
            min_x, max_x = max_x, min_x
        if min_y > max_y:
            min_y, max_y = max_y, min_y

        db.upsert_unit(
            ExcavationUnit(
                name=name,
                shape=shape_raw,
                min_x=min_x,
                min_y=min_y,
                max_x=max_x,
                max_y=max_y,
                center_x=center_x,
                center_y=center_y,
                radius=radius,
            )
        )
        u_count += 1

        # Extract any unit-scoped carryover columns present in legacy EDM_Units
        carry_state: Dict[str, str] = {}
        for k, v in row.items():
            if k is None or v is None:
                continue
            k_clean = str(k).strip()
            if k_clean.upper() not in reserved_unit_cols and str(v).strip() != "":
                carry_state[k_clean] = str(v).strip()
        if carry_state:
            db.save_unit_field_state(name, carry_state)
            state_count += 1

    return u_count, state_count


def import_points_rows(db: EDMDatabase, rows: Iterable[Dict[str, Any]]) -> int:
    """
    Imports point records from either modern CSV headers or legacy EDMWin PointTable columns.
    Preserves all custom excavation fields (CODE, LEVEL, EXCAVATOR, etc.) inside extra_fields_json.
    """
    reserved_point_cols = {
        "DOC_ID", "RECNO", "RECORD", "OBJECTID",
        "UNIT", "ID", "SUFFIX",
        "X", "Y", "Z",
        "PRISM", "PRISM_NAME", "PRISMNAME",
        "PRISM_HT", "PRISM_HEIGHT", "POLEHEIGHT",
        "PRISM_OFFSET", "PRISMOFFSET",
        "SLOPE_DIST", "SLOPED", "SLOPEDIST",
        "H_ANGLE", "HANGLE", "H_ANGLE_DEG",
        "V_ANGLE", "VANGLE", "V_ANGLE_DEG",
        "STATION_X", "STATIONX", "DATUMX",
        "STATION_Y", "STATIONY", "DATUMY",
        "STATION_Z", "STATIONZ", "DATUMZ",
        "DATUMNAME", "STATIONNAME",
        "TIMESTAMP", "DATETIME",
    }
    base_stn = db.load_station_state()
    count = 0

    for row in rows:
        x_raw = _ci_get(row, "X", default="")
        y_raw = _ci_get(row, "Y", default="")
        z_raw = _ci_get(row, "Z", default="")
        if x_raw == "" and y_raw == "" and z_raw == "":
            continue

        unit = str(_ci_get(row, "UNIT", default="")).strip()
        pt_id = str(_ci_get(row, "ID", default="1")).strip()
        suffix = _safe_int(_ci_get(row, "SUFFIX", default=0))
        x = _safe_float(x_raw)
        y = _safe_float(y_raw)
        z = _safe_float(z_raw)

        prism_name = str(_ci_get(row, "PRISM", "PRISM_NAME", "PRISMNAME", default="Imported")).strip()
        prism_ht = _safe_float(_ci_get(row, "PRISM_HT", "PRISM_HEIGHT", "POLEHEIGHT", default=0.0))
        prism_off = _safe_float(_ci_get(row, "PRISM_OFFSET", "PRISMOFFSET", default=0.0))
        prism = Prism(name=prism_name or "Imported", height=prism_ht, offset=prism_off)

        slope_dist = _safe_float(_ci_get(row, "SLOPE_DIST", "SLOPED", "SLOPEDIST", default=0.0))
        h_angle = _safe_float(_ci_get(row, "H_ANGLE", "HANGLE", "H_ANGLE_DEG", default=0.0))
        v_angle = _safe_float(_ci_get(row, "V_ANGLE", "VANGLE", "V_ANGLE_DEG", default=0.0))

        stn_name = str(_ci_get(row, "DATUMNAME", "STATIONNAME", default=base_stn.station_name)).strip()
        stn_x = _safe_float(_ci_get(row, "STATION_X", "STATIONX", "DATUMX", default=base_stn.x))
        stn_y = _safe_float(_ci_get(row, "STATION_Y", "STATIONY", "DATUMY", default=base_stn.y))
        stn_z_ctr = _safe_float(
            _ci_get(row, "STATION_Z", "STATIONZ", "DATUMZ", default=base_stn.instrument_center_z)
        )
        shot_stn = StationState.from_legacy_cfg(
            station_name=stn_name or base_stn.station_name,
            station_x=stn_x,
            station_y=stn_y,
            station_z_center=stn_z_ctr,
            instrument_height=base_stn.instrument_height,
        )

        ts = str(_ci_get(row, "TIMESTAMP", "DATETIME", default="")).strip()
        if not ts:
            d_part = str(_ci_get(row, "DATE", default="")).strip()
            t_part = str(_ci_get(row, "TIME", default="")).strip()
            if d_part or t_part:
                ts = f"{d_part} {t_part}".strip()

        extra: Dict[str, Any] = {}
        for k, v in row.items():
            if k is None or v is None:
                continue
            k_clean = str(k).strip()
            if k_clean.upper() not in reserved_point_cols and str(v).strip() != "":
                extra[k_clean] = str(v).strip()
        if unit:
            extra.setdefault("UNIT", unit)
        if pt_id:
            extra.setdefault("ID", pt_id)
        extra.setdefault("SUFFIX", str(suffix))

        db.record_point(
            unit=unit,
            point_id=pt_id,
            suffix=suffix,
            x=x,
            y=y,
            z=z,
            prism=prism,
            slope_dist=slope_dist,
            h_angle_deg=h_angle,
            v_angle_deg=v_angle,
            station=shot_stn,
            extra_fields=extra,
            timestamp=ts or None,
        )
        count += 1

    return count


def rebuild_unit_field_states_from_points(db: EDMDatabase) -> int:
    """
    Scans all recorded points per Excavation Unit and updates unit_field_state
    with the next numeric ID (max_numeric_id + 1) and most recent LEVEL / EXCAVATOR / CODE
    so switching to any migrated unit immediately resumes numbering at the right ID.
    """
    pts = db.list_points()
    latest_by_unit: Dict[str, Dict[str, Any]] = {}
    for p in pts:
        u = str(p.get("unit", "")).strip()
        if u:
            latest_by_unit[u.upper()] = p

    rebuilt = 0
    for u_upper, last_pt in latest_by_unit.items():
        u_name = str(last_pt.get("unit", u_upper)).strip()
        existing_state = db.load_unit_field_state(u_name)
        max_num_id = db.get_max_numeric_id_for_unit(u_name)
        next_id_str = str(max_num_id + 1) if max_num_id > 0 else str(last_pt.get("id", "1"))

        merged = dict(existing_state)
        for k, v in last_pt.get("extra_fields", {}).items():
            if k.upper() not in ("ID", "SUFFIX", "X", "Y", "Z"):
                merged.setdefault(k, str(v))
        merged["UNIT"] = u_name
        merged["ID"] = next_id_str
        merged["SUFFIX"] = "0"
        db.save_unit_field_state(u_name, merged)
        rebuilt += 1

    return rebuilt


def resolve_csv_bundle_paths(path_or_prefix: str | Path) -> Dict[str, Path]:
    """
    Given either a prefix path (e.g. 'C:/data/roc2026') or one of the 4 bundle files
    (e.g. 'C:/data/roc2026_points.csv'), resolves the paths to all 4 bundle files:
    _points.csv, _datums.csv, _prisms.csv, _units.csv.
    """
    p = Path(path_or_prefix)
    stem = p.stem
    for suffix in ("_points", "_datums", "_prisms", "_units"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    parent = p.parent
    return {
        "points": parent / f"{stem}_points.csv",
        "datums": parent / f"{stem}_datums.csv",
        "prisms": parent / f"{stem}_prisms.csv",
        "units": parent / f"{stem}_units.csv",
    }


def _read_csv_rows(csv_path: Path) -> List[Dict[str, Any]]:
    if not csv_path.is_file():
        return []
    try:
        raw = csv_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raw = csv_path.read_text(encoding="latin-1")
    reader = csv.DictReader(io.StringIO(raw))
    return [dict(r) for r in reader]


def import_edmwin_csv_bundle(db: EDMDatabase, path_or_prefix: str | Path) -> MigrationReport:
    """
    Imports an EDMWin 4-table CSV bundle (_datums.csv, _prisms.csv, _units.csv, _points.csv)
    into the target SQLite database and rebuilds unit-scoped field states.
    """
    paths = resolve_csv_bundle_paths(path_or_prefix)
    if not any(p.is_file() for p in paths.values()):
        raise FileNotFoundError(
            f"No EDMWin CSV bundle files found matching prefix: {path_or_prefix}"
        )

    report = MigrationReport(source_type="CSV_BUNDLE", source_path=str(path_or_prefix))
    report.datums_imported = import_datums_rows(db, _read_csv_rows(paths["datums"]))
    report.prisms_imported = import_prisms_rows(db, _read_csv_rows(paths["prisms"]))
    u_cnt, _ = import_units_rows(db, _read_csv_rows(paths["units"]))
    report.units_imported = u_cnt
    report.points_imported = import_points_rows(db, _read_csv_rows(paths["points"]))
    report.units_state_rebuilt = rebuild_unit_field_states_from_points(db)
    return report


# --- Microsoft Access (.mdb) Reader Backends ---

def _read_mdb_via_pyodbc(mdb_path: Path) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    try:
        import pyodbc  # type: ignore
    except ImportError:
        return None

    drivers = [
        d for d in pyodbc.drivers()
        if "access" in d.lower() and (".mdb" in d.lower() or "driver" in d.lower())
    ]
    if not drivers:
        drivers = ["Microsoft Access Driver (*.mdb, *.accdb)", "Microsoft Access Driver (*.mdb)"]

    for drv in drivers:
        conn_str = f"DRIVER={{{drv}}};DBQ={mdb_path};READONLY=TRUE;"
        try:
            conn = pyodbc.connect(conn_str, autocommit=True)
            cur = conn.cursor()
            table_names = [
                row.table_name
                for row in cur.tables(tableType="TABLE")
                if not str(row.table_name).startswith("MSys")
            ]
            tables_data: Dict[str, List[Dict[str, Any]]] = {}
            for tname in table_names:
                cur.execute(f"SELECT * FROM [{tname}]")
                cols = [col[0] for col in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                tables_data[tname] = rows
            conn.close()
            return tables_data
        except Exception:
            continue
    return None


def _read_mdb_via_mdbtools(mdb_path: Path) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    if not (shutil.which("mdb-tables") and shutil.which("mdb-export")):
        return None
    try:
        res = subprocess.run(
            ["mdb-tables", "-1", str(mdb_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        table_names = [
            line.strip()
            for line in res.stdout.splitlines()
            if line.strip() and not line.strip().startswith("MSys")
        ]
        tables_data: Dict[str, List[Dict[str, Any]]] = {}
        for tname in table_names:
            exp = subprocess.run(
                ["mdb-export", str(mdb_path), tname],
                capture_output=True,
                text=True,
                check=True,
            )
            reader = csv.DictReader(io.StringIO(exp.stdout))
            tables_data[tname] = [dict(r) for r in reader]
        return tables_data
    except Exception:
        return None


def _read_mdb_via_windows_oledb(mdb_path: Path) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """
    Zero-dependency Windows fallback using PowerShell System.Data.OleDb
    (tries both 64-bit ACE.OLEDB.12.0/16.0 and 32-bit SysWOW64 Microsoft.Jet.OLEDB.4.0).
    """
    if sys.platform != "win32":
        return None

    ps_script = r"""
param([string]$MdbPath)
$ErrorActionPreference = 'Stop'
$providers = @('Microsoft.ACE.OLEDB.16.0', 'Microsoft.ACE.OLEDB.12.0', 'Microsoft.Jet.OLEDB.4.0')
$conn = $null
foreach ($prov in $providers) {
    try {
        $c = New-Object System.Data.OleDb.OleDbConnection("Provider=$prov;Data Source=$MdbPath;Mode=Read;")
        $c.Open()
        $conn = $c
        break
    } catch {}
}
if ($null -eq $conn) { exit 2 }
$schema = $conn.GetOleDbSchemaTable([System.Data.OleDb.OleDbSchemaGuid]::Tables, @($null, $null, $null, 'TABLE'))
$result = @{}
foreach ($row in $schema.Rows) {
    $tname = [string]$row['TABLE_NAME']
    if ($tname.StartsWith('MSys')) { continue }
    $cmd = $conn.CreateCommand()
    $cmd.CommandText = "SELECT * FROM [$tname]"
    $adapter = New-Object System.Data.OleDb.OleDbDataAdapter($cmd)
    $dt = New-Object System.Data.DataTable
    [void]$adapter.Fill($dt)
    $rows = @()
    foreach ($r in $dt.Rows) {
        $obj = @{}
        foreach ($col in $dt.Columns) {
            $cname = [string]$col.ColumnName
            $val = $r[$cname]
            if ($val -is [System.DBNull]) { $obj[$cname] = '' } else { $obj[$cname] = [string]$val }
        }
        $rows += $obj
    }
    $result[$tname] = $rows
}
$conn.Close()
$result | ConvertTo-Json -Depth 5 -Compress
"""
    ps_candidates = [
        "powershell.exe",
        r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe",
    ]
    for ps_bin in ps_candidates:
        if not (shutil.which(ps_bin) or Path(ps_bin).is_file()):
            continue
        try:
            proc = subprocess.run(
                [ps_bin, "-NoProfile", "-NonInteractive", "-Command", ps_script, "-MdbPath", str(mdb_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                parsed = json.loads(proc.stdout.strip())
                if isinstance(parsed, dict):
                    return parsed
        except Exception:
            continue
    return None


def read_mdb_tables(
    mdb_path: str | Path,
    custom_reader: Optional[Callable[[Path], Dict[str, List[Dict[str, Any]]]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Extracts all user tables from a legacy EDMWin Microsoft Access (.mdb) file
    into a dictionary of {table_name: [row_dict, ...]}.
    """
    p = Path(mdb_path)
    if not p.is_file():
        raise FileNotFoundError(f"MDB file not found: {p}")

    if custom_reader is not None:
        return custom_reader(p)

    for backend in (_read_mdb_via_pyodbc, _read_mdb_via_mdbtools, _read_mdb_via_windows_oledb):
        data = backend(p)
        if data is not None:
            return data

    raise RuntimeError(
        "Unable to read .mdb file. Install 'pyodbc' (with Microsoft Access Database Engine), "
        "install 'mdbtools' (macOS/Linux), or export the 4-table CSV bundle from EDMWin."
    )


def import_mdb_tables_dict(
    db: EDMDatabase,
    tables: Dict[str, List[Dict[str, Any]]],
    source_label: str = "memory.mdb",
    points_table_hint: str = "",
) -> MigrationReport:
    """
    Maps a dictionary of extracted .mdb tables ({table_name: [rows]}) into EDMDatabase:
    - Identifies EDM_Datums / Datums
    - Identifies EDM_Poles / Prisms
    - Identifies EDM_Units / Units
    - Identifies the Points / Context table (via points_table_hint or by X/Y/Z/ID columns)
    """
    report = MigrationReport(source_type="MDB", source_path=source_label)
    consumed_tables: set[str] = set()

    for tname, rows in tables.items():
        tu = tname.strip().upper()
        if tu in ("EDM_DATUMS", "DATUMS") or tu.endswith("_DATUMS"):
            report.datums_imported += import_datums_rows(db, rows)
            consumed_tables.add(tname)
        elif tu in ("EDM_POLES", "EDM_PRISMS", "PRISMS", "POLES") or tu.endswith("_PRISMS") or tu.endswith("_POLES"):
            report.prisms_imported += import_prisms_rows(db, rows)
            consumed_tables.add(tname)
        elif tu in ("EDM_UNITS", "UNITS") or tu.endswith("_UNITS"):
            u_cnt, _ = import_units_rows(db, rows)
            report.units_imported += u_cnt
            consumed_tables.add(tname)
        elif tu in ("EDM_DEFAULTS", "EDM_SUITES", "EDM_STATIONS"):
            consumed_tables.add(tname)

    # Locate the Points table(s)
    hint_upper = points_table_hint.strip().upper()
    for tname, rows in tables.items():
        if tname in consumed_tables or not rows:
            continue
        tu = tname.strip().upper()
        sample_keys = {str(k).strip().upper() for k in rows[0].keys() if k is not None}
        is_points_table = (
            (hint_upper and tu == hint_upper)
            or tu in ("POINTS", "CONTEXT")
            or tu.endswith("_POINTS")
            or ({"X", "Y", "Z"}.issubset(sample_keys) and ("ID" in sample_keys or "UNIT" in sample_keys))
        )
        if is_points_table:
            report.points_imported += import_points_rows(db, rows)
            consumed_tables.add(tname)

    report.units_state_rebuilt = rebuild_unit_field_states_from_points(db)
    return report


def import_edmwin_mdb(
    db: EDMDatabase,
    mdb_path: str | Path,
    points_table_hint: str = "",
    custom_reader: Optional[Callable[[Path], Dict[str, List[Dict[str, Any]]]]] = None,
) -> MigrationReport:
    """Reads a legacy .mdb database and imports all Datums, Prisms, Units, and Points into SQLite."""
    tables = read_mdb_tables(mdb_path, custom_reader=custom_reader)
    return import_mdb_tables_dict(
        db,
        tables,
        source_label=str(mdb_path),
        points_table_hint=points_table_hint,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate legacy EDMWin (.mdb or 4-file CSV bundle) into Modern EDM SQLite."
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path to legacy .mdb file OR 4-file CSV bundle prefix / _points.csv file",
    )
    parser.add_argument(
        "--target-db",
        default="excavation_project.sqlite",
        help="Target SQLite database path (default: excavation_project.sqlite)",
    )
    parser.add_argument(
        "--points-table",
        default="",
        help="Optional name of the points table inside the .mdb file (e.g. Points or site name)",
    )
    args = parser.parse_args(argv)

    src_path = Path(args.source)
    db = EDMDatabase(args.target_db)
    try:
        if src_path.suffix.lower() in (".mdb", ".accdb"):
            rep = import_edmwin_mdb(db, src_path, points_table_hint=args.points_table)
        else:
            rep = import_edmwin_csv_bundle(db, src_path)
        print(rep.summary_text())
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
