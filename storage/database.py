"""
Crash-Safe Atomic SQLite Storage, Duplicate ID Resolver, Series Resequencer & CSV / GeoJSON Exporters.
Replicates all database behaviors from decompiled edmwin.exe:
- Lookup Last ID per unit (0x0042d2cc / 0x004339d4: numeric IDs where id < 'A')
- Duplicate ID detection & frmDupID 3-way resolution (Convert to alpha ID, New ID, Continuation +Shot)
- Multi-shot series initial-record verification (0x0042c274)
- Multi-shot series deletion & automatic SUFFIX resequencing (FUN_00489a60)
- Unit-scoped field state persistence (EDM_Units carryover fields)
- 4-Table Legacy EDMWin CSV Bundle Export (_points.csv, _datums.csv, _prisms.csv, _units.csv)
"""

from __future__ import annotations

import csv
import datetime
import json
import sqlite3
import string
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from survey.math_engine import Datum, ExcavationUnit, Prism, StationState


class EDMDatabase:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=FULL;")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS datums (
                name TEXT PRIMARY KEY,
                x REAL NOT NULL,
                y REAL NOT NULL,
                z REAL NOT NULL,
                description TEXT DEFAULT ''
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS prisms (
                name TEXT PRIMARY KEY,
                height REAL NOT NULL,
                offset REAL NOT NULL DEFAULT 0.0,
                description TEXT DEFAULT ''
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS units (
                name TEXT PRIMARY KEY,
                shape TEXT NOT NULL DEFAULT 'RECT',
                min_x REAL DEFAULT 0.0,
                min_y REAL DEFAULT 0.0,
                max_x REAL DEFAULT 0.0,
                max_y REAL DEFAULT 0.0,
                center_x REAL DEFAULT 0.0,
                center_y REAL DEFAULT 0.0,
                radius REAL DEFAULT 0.0,
                min_z REAL,
                max_z REAL
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS station_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                setup_method TEXT NOT NULL,
                station_name TEXT NOT NULL,
                x REAL NOT NULL,
                y REAL NOT NULL,
                z REAL NOT NULL,
                instrument_height REAL NOT NULL,
                azimuth_offset_rad REAL NOT NULL,
                rms_error_2d REAL NOT NULL DEFAULT 0.0,
                rms_error_z REAL NOT NULL DEFAULT 0.0
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS points (
                doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
                unit TEXT DEFAULT '',
                id TEXT DEFAULT '',
                suffix INTEGER DEFAULT 0,
                x REAL NOT NULL,
                y REAL NOT NULL,
                z REAL NOT NULL,
                prism_name TEXT DEFAULT '',
                prism_height REAL DEFAULT 0.0,
                prism_offset REAL DEFAULT 0.0,
                slope_dist REAL DEFAULT 0.0,
                h_angle_deg REAL DEFAULT 0.0,
                v_angle_deg REAL DEFAULT 0.0,
                station_x REAL DEFAULT 0.0,
                station_y REAL DEFAULT 0.0,
                station_z REAL DEFAULT 0.0,
                timestamp TEXT NOT NULL,
                extra_fields_json TEXT DEFAULT '{}'
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS unit_field_state (
                unit_name TEXT PRIMARY KEY,
                state_json TEXT NOT NULL
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS context_defaults (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                master_var TEXT NOT NULL DEFAULT '',
                slave_vars_json TEXT NOT NULL DEFAULT '[]',
                rules_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        cur.close()
        self.conn.commit()

        if not self.list_datums():
            self.upsert_datum(Datum("D1", 100.000, 100.000, 10.000, "Primary Grid Origin"))
            self.upsert_datum(Datum("D2", 100.000, 110.000, 10.250, "North Backsight Nail"))
            self.upsert_datum(Datum("D3", 110.000, 100.000, 9.850, "East Control Nail"))
        if not self.list_prisms():
            self.upsert_prism(Prism("Mini Prism", 0.100, 0.017, "10cm Pin Pole (17mm offset)"))
            self.upsert_prism(Prism("Standard Pole", 1.500, -0.030, "1.5m Survey Rod (-30mm offset)"))
            self.upsert_prism(Prism("Reflectorless", 0.000, 0.000, "Laser Direct"))
        if not self.list_units():
            self.upsert_unit(
                ExcavationUnit(
                    name="N100E100",
                    shape="RECT",
                    min_x=100.0,
                    min_y=100.0,
                    max_x=102.0,
                    max_y=102.0,
                )
            )

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            self.conn.execute("PRAGMA journal_mode=DELETE;")
            self.conn.commit()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass

    def upsert_datum(self, datum: Datum) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO datums (name, x, y, z, description)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    x=excluded.x, y=excluded.y, z=excluded.z, description=excluded.description;
                """,
                (datum.name, datum.x, datum.y, datum.z, datum.description),
            )

    def delete_datum(self, name: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM datums WHERE name = ?;", (name,))

    def list_datums(self) -> List[Datum]:
        rows = self.conn.execute("SELECT * FROM datums ORDER BY name;").fetchall()
        return [Datum(r["name"], r["x"], r["y"], r["z"], r["description"]) for r in rows]

    def upsert_prism(self, prism: Prism) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO prisms (name, height, offset, description)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    height=excluded.height, offset=excluded.offset, description=excluded.description;
                """,
                (prism.name, prism.height, prism.offset, prism.description),
            )

    def delete_prism(self, name: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM prisms WHERE name = ?;", (name,))

    def list_prisms(self) -> List[Prism]:
        rows = self.conn.execute("SELECT * FROM prisms ORDER BY name;").fetchall()
        return [Prism(r["name"], r["height"], r["offset"], r["description"]) for r in rows]

    def upsert_unit(self, unit: ExcavationUnit) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO units (name, shape, min_x, min_y, max_x, max_y, center_x, center_y, radius, min_z, max_z)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    shape=excluded.shape, min_x=excluded.min_x, min_y=excluded.min_y,
                    max_x=excluded.max_x, max_y=excluded.max_y, center_x=excluded.center_x,
                    center_y=excluded.center_y, radius=excluded.radius,
                    min_z=excluded.min_z, max_z=excluded.max_z;
                """,
                (
                    unit.name,
                    unit.shape,
                    unit.min_x,
                    unit.min_y,
                    unit.max_x,
                    unit.max_y,
                    unit.center_x,
                    unit.center_y,
                    unit.radius,
                    unit.min_z,
                    unit.max_z,
                ),
            )

    def delete_unit(self, name: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM units WHERE name = ?;", (name,))

    def list_units(self) -> List[ExcavationUnit]:
        rows = self.conn.execute("SELECT * FROM units ORDER BY name;").fetchall()
        return [
            ExcavationUnit(
                name=r["name"],
                shape=r["shape"],
                min_x=r["min_x"],
                min_y=r["min_y"],
                max_x=r["max_x"],
                max_y=r["max_y"],
                center_x=r["center_x"],
                center_y=r["center_y"],
                radius=r["radius"],
                min_z=r["min_z"],
                max_z=r["max_z"],
            )
            for r in rows
        ]

    def save_station_state(self, state: StationState) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO station_state (
                    id, setup_method, station_name, x, y, z,
                    instrument_height, azimuth_offset_rad, rms_error_2d, rms_error_z
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    setup_method=excluded.setup_method,
                    station_name=excluded.station_name,
                    x=excluded.x, y=excluded.y, z=excluded.z,
                    instrument_height=excluded.instrument_height,
                    azimuth_offset_rad=excluded.azimuth_offset_rad,
                    rms_error_2d=excluded.rms_error_2d,
                    rms_error_z=excluded.rms_error_z;
                """,
                (
                    state.setup_method,
                    state.station_name,
                    state.x,
                    state.y,
                    state.z,
                    state.instrument_height,
                    state.azimuth_offset_rad,
                    state.rms_error_2d,
                    state.rms_error_z,
                ),
            )

    def load_station_state(self) -> StationState:
        row = self.conn.execute("SELECT * FROM station_state WHERE id = 1;").fetchone()
        if not row:
            default_state = StationState()
            self.save_station_state(default_state)
            return default_state
        return StationState(
            setup_method=row["setup_method"],
            station_name=row["station_name"],
            x=row["x"],
            y=row["y"],
            z=row["z"],
            instrument_height=row["instrument_height"],
            azimuth_offset_rad=row["azimuth_offset_rad"],
            rms_error_2d=row["rms_error_2d"],
            rms_error_z=row["rms_error_z"],
        )

    # --- Duplicate ID & Multi-Shot Series Validation (frmDupID & FUN_00465a60 / FUN_00489a60) ---

    def get_max_numeric_id_for_unit(self, unit: str) -> int:
        """
        Replicates 'select max(id) from context where id<\"A\" and unit=...' (0x0042d2cc / 0x004339d4):
        Finds the highest purely numeric ID recorded for the given unit.
        """
        rows = self.conn.execute(
            "SELECT id FROM points WHERE UPPER(unit) = UPPER(?);",
            (unit.strip(),),
        ).fetchall()
        max_id = 0
        for r in rows:
            val = str(r["id"]).strip()
            if val.isdigit():
                max_id = max(max_id, int(val))
        return max_id

    def has_initial_series_shot(self, unit: str, point_id: str) -> bool:
        """
        Replicates 0x0042c274 ('No initial record in this series has been recorded. Shoot as new object'):
        Checks whether at least one shot (e.g. SUFFIX=0) exists for (unit, point_id).
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM points WHERE UPPER(unit) = UPPER(?) AND UPPER(id) = UPPER(?);",
            (unit.strip(), str(point_id).strip()),
        ).fetchone()
        return bool(row and int(row["cnt"]) > 0)

    def check_duplicate_id(self, unit: str, point_id: str, suffix: int = 0) -> bool:
        """Checks if (unit, id, suffix) already exists in the points table."""
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM points
            WHERE UPPER(unit) = UPPER(?) AND UPPER(id) = UPPER(?) AND suffix = ?;
            """,
            (unit.strip(), str(point_id).strip(), int(suffix)),
        ).fetchone()
        return bool(row and int(row["cnt"]) > 0)

    def resolve_duplicate_id(self, unit: str, point_id: str, option: str) -> Tuple[str, int]:
        """
        Replicates frmDupID (0x004399e8):
        1. 'ALPHA': Convert to alpha ID (e.g. '42' -> '42a', '42b', ...), suffix = 0
        2. 'NEW_ID': New ID of max_numeric_id + 1, suffix = 0
        3. 'CONTINUE': Continuation (+ Shot) of point_id, suffix = max_suffix + 1
        """
        opt = option.strip().upper()
        u_clean = unit.strip()
        id_clean = str(point_id).strip()

        if opt == "ALPHA":
            base_id = id_clean.rstrip(string.ascii_letters) or id_clean
            for ch in string.ascii_lowercase:
                candidate = f"{base_id}{ch}"
                if not self.check_duplicate_id(u_clean, candidate, 0):
                    return candidate, 0
            return f"{base_id}z1", 0

        if opt == "NEW_ID":
            next_num = self.get_max_numeric_id_for_unit(u_clean) + 1
            return str(next_num), 0

        if opt == "CONTINUE":
            row = self.conn.execute(
                """
                SELECT MAX(suffix) AS max_suf FROM points
                WHERE UPPER(unit) = UPPER(?) AND UPPER(id) = UPPER(?);
                """,
                (u_clean, id_clean),
            ).fetchone()
            next_suf = (int(row["max_suf"]) + 1) if (row and row["max_suf"] is not None) else 1
            return id_clean, next_suf

        raise ValueError(f"Unknown duplicate ID resolution option: {option!r}")

    def delete_point_with_resequencing(
        self,
        doc_id: int,
        delete_entire_series: bool = False,
    ) -> Dict[str, Any]:
        """
        Replicates FUN_00489a60 (DeleteRecord in edmwin.exe):
        - If delete_entire_series is True, deletes all records matching (unit, id).
        - If delete_entire_series is False, deletes the specified doc_id and resequences
          all subsequent shots in the same (unit, id) series by decrementing suffix by 1
          ('Resequencing subsequent shot(s) from ... as continuation shots for ...').
        """
        target = self.conn.execute(
            "SELECT * FROM points WHERE doc_id = ?;", (doc_id,)
        ).fetchone()
        if not target:
            return {"deleted_count": 0, "resequenced_count": 0}

        unit_val = target["unit"]
        id_val = target["id"]
        suf_val = int(target["suffix"])

        with self.conn:
            if delete_entire_series:
                cur = self.conn.execute(
                    "DELETE FROM points WHERE UPPER(unit) = UPPER(?) AND UPPER(id) = UPPER(?);",
                    (unit_val, id_val),
                )
                return {
                    "deleted_count": cur.rowcount,
                    "resequenced_count": 0,
                    "unit": unit_val,
                    "id": id_val,
                }

            self.conn.execute("DELETE FROM points WHERE doc_id = ?;", (doc_id,))
            later_rows = self.conn.execute(
                """
                SELECT doc_id, suffix, extra_fields_json FROM points
                WHERE UPPER(unit) = UPPER(?) AND UPPER(id) = UPPER(?) AND suffix > ?
                ORDER BY suffix ASC;
                """,
                (unit_val, id_val, suf_val),
            ).fetchall()

            reseq_count = 0
            for r in later_rows:
                new_suf = int(r["suffix"]) - 1
                extra = json.loads(r["extra_fields_json"] or "{}")
                if "SUFFIX" in extra:
                    extra["SUFFIX"] = str(new_suf)
                self.conn.execute(
                    "UPDATE points SET suffix = ?, extra_fields_json = ? WHERE doc_id = ?;",
                    (new_suf, json.dumps(extra), r["doc_id"]),
                )
                reseq_count += 1

            return {
                "deleted_count": 1,
                "resequenced_count": reseq_count,
                "unit": unit_val,
                "id": id_val,
            }

    def record_point(
        self,
        unit: str,
        point_id: str,
        suffix: int,
        x: float,
        y: float,
        z: float,
        prism: Prism,
        slope_dist: float,
        h_angle_deg: float,
        v_angle_deg: float,
        station: StationState,
        extra_fields: Dict[str, Any],
        timestamp: Optional[str] = None,
    ) -> int:
        ts = timestamp or datetime.datetime.now().isoformat(timespec="seconds")
        enriched_extra = dict(extra_fields)
        enriched_extra.setdefault("DATUMNAME", station.station_name)
        enriched_extra.setdefault("DATUMX", f"{station.x:.3f}")
        enriched_extra.setdefault("DATUMY", f"{station.y:.3f}")
        enriched_extra.setdefault("DATUMZ", f"{station.instrument_center_z:.3f}")
        enriched_extra.setdefault("PRISM", prism.name)

        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO points (
                    unit, id, suffix, x, y, z,
                    prism_name, prism_height, prism_offset,
                    slope_dist, h_angle_deg, v_angle_deg,
                    station_x, station_y, station_z,
                    timestamp, extra_fields_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    unit,
                    str(point_id),
                    int(suffix),
                    x,
                    y,
                    z,
                    prism.name,
                    prism.height,
                    prism.offset,
                    slope_dist,
                    h_angle_deg,
                    v_angle_deg,
                    station.x,
                    station.y,
                    station.instrument_center_z,
                    ts,
                    json.dumps(enriched_extra),
                ),
            )
            return int(cur.lastrowid)

    def list_points(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM points ORDER BY doc_id ASC;").fetchall()
        results: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            extra = json.loads(d.pop("extra_fields_json", "{}") or "{}")
            d["extra_fields"] = extra
            results.append(d)
        return results

    def get_last_point(self) -> Optional[Dict[str, Any]]:
        pts = self.list_points()
        return pts[-1] if pts else None

    def get_distinct_field_values(self, field_name: str) -> List[str]:
        """
        Replicates 'select distinct [<field>] from [<PointTable>]' in frmFilter (0x00437f9c):
        Returns sorted distinct non-empty string representations for any core column or custom .CFG field.
        """
        fname_upper = field_name.strip().upper()
        core_map = {
            "RECNO": "doc_id",
            "DOC_ID": "doc_id",
            "UNIT": "unit",
            "ID": "id",
            "SUFFIX": "suffix",
            "X": "x",
            "Y": "y",
            "Z": "z",
            "PRISM": "prism_name",
            "PRISM_NAME": "prism_name",
            "PRISM_HT": "prism_height",
            "PRISM_HEIGHT": "prism_height",
            "PRISM_OFFSET": "prism_offset",
            "SLOPED": "slope_dist",
            "SLOPE_DIST": "slope_dist",
            "HANGLE": "h_angle_deg",
            "H_ANGLE_DEG": "h_angle_deg",
            "VANGLE": "v_angle_deg",
            "V_ANGLE_DEG": "v_angle_deg",
            "STATION_X": "station_x",
            "STATION_Y": "station_y",
            "STATION_Z": "station_z",
            "TIMESTAMP": "timestamp",
        }
        vals: set[str] = set()
        for p in self.list_points():
            if fname_upper in core_map:
                raw = p.get(core_map[fname_upper], "")
                if isinstance(raw, float):
                    s = f"{raw:.4f}"
                else:
                    s = str(raw).strip()
            else:
                extra = p.get("extra_fields", {})
                s = ""
                for k, v in extra.items():
                    if k.upper() == fname_upper:
                        s = str(v).strip()
                        break
            if s:
                vals.add(s)
        return sorted(vals)

    def filter_points(
        self,
        field_name: Optional[str] = None,
        field_value: Optional[str] = None,
        station_xyz: Optional[Tuple[float, float, float]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Filters recorded points by (field_name == field_value) and/or by Current Station coordinates
        (replicating mnuFilter 'Show only Points from Current Station' @ 0x0041103a and frmFilter @ 0x00439638).
        """
        pts = self.list_points()
        if station_xyz is not None:
            sx, sy, sz = station_xyz
            pts = [
                p for p in pts
                if abs(float(p.get("station_x", 0.0)) - sx) < 1e-3
                and abs(float(p.get("station_y", 0.0)) - sy) < 1e-3
                and abs(float(p.get("station_z", 0.0)) - sz) < 1e-3
            ]

        if not field_name or field_value is None:
            return pts

        fname_upper = field_name.strip().upper()
        target_val = str(field_value).strip().upper()
        core_map = {
            "RECNO": "doc_id",
            "DOC_ID": "doc_id",
            "UNIT": "unit",
            "ID": "id",
            "SUFFIX": "suffix",
            "X": "x",
            "Y": "y",
            "Z": "z",
            "PRISM": "prism_name",
            "PRISM_NAME": "prism_name",
            "PRISM_HT": "prism_height",
            "SLOPED": "slope_dist",
            "SLOPE_DIST": "slope_dist",
            "HANGLE": "h_angle_deg",
            "VANGLE": "v_angle_deg",
            "TIMESTAMP": "timestamp",
        }

        filtered: List[Dict[str, Any]] = []
        for p in pts:
            if fname_upper in core_map:
                raw = p.get(core_map[fname_upper], "")
                s = f"{raw:.4f}" if isinstance(raw, float) else str(raw).strip()
            else:
                extra = p.get("extra_fields", {})
                s = ""
                for k, v in extra.items():
                    if k.upper() == fname_upper:
                        s = str(v).strip()
                        break
            if s.upper() == target_val:
                filtered.append(p)
        return filtered

    def update_point_cell(self, doc_id: int, column_name: str, new_value: Any) -> None:
        """
        Inline-updates any core column or custom .CFG extra field for a given point record
        (replicating MSDataGrid inline cell editing on frmDataGrid @ 0x0043b8f4).
        """
        target = self.conn.execute(
            "SELECT * FROM points WHERE doc_id = ?;", (doc_id,)
        ).fetchone()
        if not target:
            raise ValueError("Record not found")

        col_upper = column_name.strip().upper()
        extra = json.loads(target["extra_fields_json"] or "{}")

        with self.conn:
            if col_upper == "UNIT":
                val_str = str(new_value).strip()
                extra["UNIT"] = val_str
                self.conn.execute(
                    "UPDATE points SET unit = ?, extra_fields_json = ? WHERE doc_id = ?;",
                    (val_str, json.dumps(extra), doc_id),
                )
            elif col_upper == "ID":
                val_str = str(new_value).strip()
                extra["ID"] = val_str
                self.conn.execute(
                    "UPDATE points SET id = ?, extra_fields_json = ? WHERE doc_id = ?;",
                    (val_str, json.dumps(extra), doc_id),
                )
            elif col_upper == "SUFFIX":
                suf_int = int(float(str(new_value).strip() or "0"))
                extra["SUFFIX"] = str(suf_int)
                self.conn.execute(
                    "UPDATE points SET suffix = ?, extra_fields_json = ? WHERE doc_id = ?;",
                    (suf_int, json.dumps(extra), doc_id),
                )
            elif col_upper in ("X", "Y", "Z"):
                f_val = float(str(new_value).strip())
                col_sql = col_upper.lower()
                self.conn.execute(
                    f"UPDATE points SET {col_sql} = ? WHERE doc_id = ?;",
                    (f_val, doc_id),
                )
            elif col_upper in ("PRISM", "PRISM_NAME"):
                val_str = str(new_value).strip()
                extra["PRISM"] = val_str
                self.conn.execute(
                    "UPDATE points SET prism_name = ?, extra_fields_json = ? WHERE doc_id = ?;",
                    (val_str, json.dumps(extra), doc_id),
                )
            elif col_upper in ("PRISM_HT", "PRISM_HEIGHT"):
                f_val = float(str(new_value).strip())
                self.conn.execute(
                    "UPDATE points SET prism_height = ? WHERE doc_id = ?;",
                    (f_val, doc_id),
                )
            elif col_upper in ("SLOPED", "SLOPE_DIST"):
                f_val = float(str(new_value).strip())
                self.conn.execute(
                    "UPDATE points SET slope_dist = ? WHERE doc_id = ?;",
                    (f_val, doc_id),
                )
            elif col_upper in ("HANGLE", "H_ANGLE", "H_ANGLE_DEG"):
                f_val = float(str(new_value).strip())
                self.conn.execute(
                    "UPDATE points SET h_angle_deg = ? WHERE doc_id = ?;",
                    (f_val, doc_id),
                )
            elif col_upper in ("VANGLE", "V_ANGLE", "V_ANGLE_DEG"):
                f_val = float(str(new_value).strip())
                self.conn.execute(
                    "UPDATE points SET v_angle_deg = ? WHERE doc_id = ?;",
                    (f_val, doc_id),
                )
            else:
                matched_key = column_name.strip()
                for k in list(extra.keys()):
                    if k.upper() == col_upper:
                        matched_key = k
                        break
                extra[matched_key] = str(new_value).strip()
                self.conn.execute(
                    "UPDATE points SET extra_fields_json = ? WHERE doc_id = ?;",
                    (json.dumps(extra), doc_id),
                )

    def update_point_coords(self, doc_id: int, x: float, y: float, z: float) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE points SET x = ?, y = ?, z = ? WHERE doc_id = ?;",
                (x, y, z, doc_id),
            )

    def update_point_fields(self, doc_id: int, unit: str, point_id: str, suffix: int, extra_fields: Dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE points
                SET unit = ?, id = ?, suffix = ?, extra_fields_json = ?
                WHERE doc_id = ?;
                """,
                (unit, str(point_id), int(suffix), json.dumps(extra_fields), doc_id),
            )

    def delete_last_point(self) -> Optional[int]:
        row = self.conn.execute("SELECT doc_id FROM points ORDER BY doc_id DESC LIMIT 1;").fetchone()
        if not row:
            return None
        doc_id = int(row["doc_id"])
        self.delete_point_with_resequencing(doc_id, delete_entire_series=False)
        return doc_id

    def export_points_csv(self, csv_path: str | Path, extra_field_names: Optional[List[str]] = None) -> None:
        pts = self.list_points()
        if extra_field_names is None:
            keys_seen: List[str] = []
            for p in pts:
                for k in p["extra_fields"].keys():
                    if k not in keys_seen and k.upper() not in ("UNIT", "ID", "SUFFIX"):
                        keys_seen.append(k)
            extra_field_names = keys_seen

        base_headers = [
            "doc_id", "UNIT", "ID", "SUFFIX", "X", "Y", "Z",
            "PRISM", "PRISM_HT", "SLOPE_DIST", "H_ANGLE", "V_ANGLE", "TIMESTAMP"
        ]
        headers = base_headers + extra_field_names

        with Path(csv_path).open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            for p in pts:
                row_dict: Dict[str, Any] = {
                    "doc_id": p["doc_id"],
                    "UNIT": p["unit"],
                    "ID": p["id"],
                    "SUFFIX": p["suffix"],
                    "X": f"{p['x']:.4f}",
                    "Y": f"{p['y']:.4f}",
                    "Z": f"{p['z']:.4f}",
                    "PRISM": p["prism_name"],
                    "PRISM_HT": f"{p['prism_height']:.3f}",
                    "SLOPE_DIST": f"{p['slope_dist']:.4f}",
                    "H_ANGLE": f"{p['h_angle_deg']:.4f}",
                    "V_ANGLE": f"{p['v_angle_deg']:.4f}",
                    "TIMESTAMP": p["timestamp"],
                }
                for k in extra_field_names:
                    row_dict[k] = p["extra_fields"].get(k, "")
                writer.writerow(row_dict)

    def import_points_csv(self, csv_path: str | Path) -> int:
        count = 0
        reserved = {
            "doc_id", "UNIT", "ID", "SUFFIX", "X", "Y", "Z",
            "PRISM", "PRISM_HT", "SLOPE_DIST", "H_ANGLE", "V_ANGLE", "TIMESTAMP"
        }
        stn = self.load_station_state()
        with Path(csv_path).open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                extra = {k: v for k, v in row.items() if k not in reserved}
                prism = Prism(
                    name=row.get("PRISM", "Imported"),
                    height=float(row.get("PRISM_HT", 0.0) or 0.0),
                    offset=0.0,
                )
                self.record_point(
                    unit=row.get("UNIT", ""),
                    point_id=row.get("ID", ""),
                    suffix=int(row.get("SUFFIX", 0) or 0),
                    x=float(row["X"]),
                    y=float(row["Y"]),
                    z=float(row["Z"]),
                    prism=prism,
                    slope_dist=float(row.get("SLOPE_DIST", 0.0) or 0.0),
                    h_angle_deg=float(row.get("H_ANGLE", 0.0) or 0.0),
                    v_angle_deg=float(row.get("V_ANGLE", 0.0) or 0.0),
                    station=stn,
                    extra_fields=extra,
                    timestamp=row.get("TIMESTAMP"),
                )
                count += 1
        return count

    def export_geojson(self, output_path: str | Path) -> Dict[str, Any]:
        pts = self.list_points()
        features: List[Dict[str, Any]] = []
        grouped_sequences: Dict[str, List[Dict[str, Any]]] = {}

        for p in pts:
            props = {
                "doc_id": p["doc_id"],
                "UNIT": p["unit"],
                "ID": p["id"],
                "SUFFIX": p["suffix"],
                "Z": round(p["z"], 4),
                "PRISM": p["prism_name"],
                "TIMESTAMP": p["timestamp"],
                **p["extra_fields"],
            }
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [round(p["x"], 4), round(p["y"], 4), round(p["z"], 4)],
                    },
                    "properties": props,
                }
            )
            seq_key = f"{p['unit']}::{p['id']}"
            grouped_sequences.setdefault(seq_key, []).append(p)

        for seq_key, group in grouped_sequences.items():
            if len(group) >= 2:
                sorted_grp = sorted(group, key=lambda item: int(item["suffix"]))
                coords = [
                    [round(g["x"], 4), round(g["y"], 4), round(g["z"], 4)]
                    for g in sorted_grp
                ]
                first = sorted_grp[0]
                features.append(
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "LineString",
                            "coordinates": coords,
                        },
                        "properties": {
                            "layer_type": "MultiShotLine",
                            "UNIT": first["unit"],
                            "ID": first["id"],
                            "shot_count": len(sorted_grp),
                            **first["extra_fields"],
                        },
                    }
                )

        collection = {
            "type": "FeatureCollection",
            "name": "EDM_Excavation_Export",
            "features": features,
        }
        Path(output_path).write_text(json.dumps(collection, indent=2), encoding="utf-8")
        return collection

    # --- Unit-Scoped Field State (EDMWin / EDMCE UnitFields Parity) ---
    def save_unit_field_state(self, unit_name: str, state_dict: Dict[str, Any]) -> None:
        if not unit_name:
            return
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO unit_field_state (unit_name, state_json)
                VALUES (?, ?)
                ON CONFLICT(unit_name) DO UPDATE SET state_json = excluded.state_json;
                """,
                (unit_name.strip().upper(), json.dumps(state_dict)),
            )

    def load_unit_field_state(self, unit_name: str) -> Dict[str, Any]:
        if not unit_name:
            return {}
        row = self.conn.execute(
            "SELECT state_json FROM unit_field_state WHERE unit_name = ?;",
            (unit_name.strip().upper(),),
        ).fetchone()
        return json.loads(row["state_json"]) if row else {}

    # --- Full 4-Table Legacy EDMWin v3.3 ASCII / CSV Bundle Export & Import ---
    def export_edmwin_csv_bundle(self, root_path: str | Path) -> Dict[str, str]:
        """
        Matches EDMWin's 'File -> Export CSVs' (frmExport_CSVs @ 0x0043ab48) which takes
        a folder and file prefix and appends _points, _prisms, _units, _datums:
          - <prefix>_points.csv
          - <prefix>_datums.csv
          - <prefix>_prisms.csv
          - <prefix>_units.csv
        """
        root = Path(root_path)
        stem = root.stem
        parent = root.parent

        pts_path = parent / f"{stem}_points.csv"
        dat_path = parent / f"{stem}_datums.csv"
        prs_path = parent / f"{stem}_prisms.csv"
        unt_path = parent / f"{stem}_units.csv"

        self.export_points_csv(pts_path)

        with dat_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["NAME", "X", "Y", "Z", "DESCRIPTION"])
            for d in self.list_datums():
                w.writerow([d.name, f"{d.x:.4f}", f"{d.y:.4f}", f"{d.z:.4f}", d.description])

        with prs_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["NAME", "HEIGHT", "OFFSET", "DESCRIPTION"])
            for p in self.list_prisms():
                w.writerow([p.name, f"{p.height:.3f}", f"{p.offset:.3f}", p.description])

        with unt_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["NAME", "SHAPE", "MIN_X", "MIN_Y", "MAX_X", "MAX_Y", "CENTER_X", "CENTER_Y", "RADIUS"])
            for u in self.list_units():
                w.writerow([
                    u.name, u.shape,
                    f"{u.min_x:.3f}", f"{u.min_y:.3f}", f"{u.max_x:.3f}", f"{u.max_y:.3f}",
                    f"{u.center_x:.3f}", f"{u.center_y:.3f}", f"{u.radius:.3f}",
                ])

        return {
            "points": str(pts_path),
            "datums": str(dat_path),
            "prisms": str(prs_path),
            "units": str(unt_path),
        }

    def import_edmwin_csv_bundle(self, path_or_prefix: str | Path) -> Dict[str, int]:
        """
        Imports an EDMWin 4-table CSV bundle (_points.csv, _datums.csv, _prisms.csv, _units.csv)
        and rebuilds unit-scoped field state.
        """
        from storage.legacy_migrator import import_edmwin_csv_bundle as _imp_bundle
        rep = _imp_bundle(self, path_or_prefix)
        return {
            "points": rep.points_imported,
            "datums": rep.datums_imported,
            "prisms": rep.prisms_imported,
            "units": rep.units_imported,
            "unit_states": rep.units_state_rebuilt,
        }

    def import_edmwin_mdb(self, mdb_path: str | Path, points_table_hint: str = "") -> Dict[str, int]:
        """
        Imports all Datums, Prisms, Units, and Points from a legacy EDMWin Microsoft Access (.mdb)
        file into this SQLite database and rebuilds unit-scoped field state.
        """
        from storage.legacy_migrator import import_edmwin_mdb as _imp_mdb
        rep = _imp_mdb(self, mdb_path, points_table_hint=points_table_hint)
        return {
            "points": rep.points_imported,
            "datums": rep.datums_imported,
            "prisms": rep.prisms_imported,
            "units": rep.units_imported,
            "unit_states": rep.units_state_rebuilt,
        }

    # --- Context-Dependent Defaults (EDM_Defaults / frmSubUnits Parity) ---
    def save_context_defaults(
        self,
        master_var: str,
        slave_vars: List[str],
        rules: Dict[str, Dict[str, str]],
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO context_defaults (id, master_var, slave_vars_json, rules_json)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    master_var = excluded.master_var,
                    slave_vars_json = excluded.slave_vars_json,
                    rules_json = excluded.rules_json;
                """,
                (
                    master_var.strip(),
                    json.dumps(list(slave_vars)),
                    json.dumps(rules),
                ),
            )

    def load_context_defaults(self) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT master_var, slave_vars_json, rules_json FROM context_defaults WHERE id = 1;"
        ).fetchone()
        if not row:
            return {"master_var": "", "slave_vars": [], "rules": {}}
        return {
            "master_var": row["master_var"] or "",
            "slave_vars": json.loads(row["slave_vars_json"] or "[]"),
            "rules": json.loads(row["rules_json"] or "{}"),
        }

    def clear_context_defaults(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM context_defaults WHERE id = 1;")

    def update_context_default_rule(
        self,
        master_val: str,
        current_form_values: Dict[str, Any],
    ) -> None:
        ctx = self.load_context_defaults()
        master_var = ctx["master_var"]
        slave_vars = ctx["slave_vars"]
        if not master_var or not slave_vars:
            return
        m_key = str(master_val).strip().upper()
        if not m_key:
            return
        rules = ctx["rules"]
        entry = dict(rules.get(m_key, {}))
        for sv in slave_vars:
            for k, v in current_form_values.items():
                if k.upper() == sv.upper():
                    entry[sv] = str(v).strip()
                    break
        rules[m_key] = entry
        self.save_context_defaults(master_var, slave_vars, rules)

    def get_context_dependent_values(self, master_val: str) -> Dict[str, str]:
        ctx = self.load_context_defaults()
        m_key = str(master_val).strip().upper()
        if not ctx["master_var"] or not m_key:
            return {}
        return dict(ctx["rules"].get(m_key, {}))
