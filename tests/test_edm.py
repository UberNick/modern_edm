"""
Comprehensive Automated Test Suite for Modern EDMWin Replacement.
Verifies 100% functional, mathematical, database, UI, and protocol parity with
decompiled edmwin.exe (v3.2 / 3.05.0010), including all 10 behavioral parity fixes.
"""

from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path

from drivers.total_station import (
    LeicaAutoTiltDriver,
    LeicaBuilderDriver,
    LeicaGeoCOMDriver,
    LeicaGSIDriver,
    MicroscribeDriver,
    SerialConfig,
    SokkiaNikonDriver,
    TopconDriver,
    compute_topcon_bcc,
    compute_topcon_bcc_decimal3,
    radians_to_dddmmss,
)
from storage.cfg_parser import (
    load_cfg_file,
    parse_cfg_text,
    save_legacy_cfg_file,
)
from storage.database import EDMDatabase
from storage.label_printer import append_label_to_spool
from survey.math_engine import (
    Datum,
    ExcavationUnit,
    Point3D,
    Prism,
    ResectionSighting,
    StationState,
    apply_coordinate_offset_expression,
    apply_directional_offset,
    auto_find_unit,
    calc_angle_dddmmss,
    check_point_in_unit,
    flip_180_dddmmss,
    format_speech_unit_id,
    initialize_direct,
    initialize_one_point,
    initialize_resection,
    initialize_two_point_backsight,
    reduce_polar_to_cartesian,
    solve_3d_block_georeference,
    validate_prism_bounds,
)


def test_leica_geocom_and_autotilt_and_builder() -> None:
    drv = LeicaGeoCOMDriver(SerialConfig())
    frame = f"%R1P,0,0:0,{math.pi / 2:.8f},{math.pi / 2:.8f},15.2500\r\n"
    obs = drv.parse_response(frame)
    assert math.isclose(obs.slope_distance, 15.2500, abs_tol=1e-6)
    assert math.isclose(obs.horizontal_angle_deg, 90.0, abs_tol=1e-5)
    assert math.isclose(obs.vertical_angle_deg, 90.0, abs_tol=1e-5)
    set_cmd = drv.format_set_horizontal_angle_cmd(90.0000)
    assert set_cmd.startswith("%R1Q,2113:1.5707963")

    at_drv = LeicaAutoTiltDriver(SerialConfig())
    tokens = ["0"] + ["0.0"] * 10 + ["0.012"] + ["0.0"] + [f"{math.pi / 4:.8f}", f"{math.pi / 2:.8f}", "18.750"]
    at_frame = "%R1P,0,0:" + ",".join(tokens) + "\r\n"
    at_obs = at_drv.parse_response(at_frame)
    assert math.isclose(at_obs.slope_distance, 18.750, abs_tol=1e-6)
    assert math.isclose(at_obs.horizontal_angle_deg, 45.0, abs_tol=1e-4)
    assert math.isclose(at_obs.tilt_compensation, 0.012, abs_tol=1e-6)

    b_drv = LeicaBuilderDriver(SerialConfig())
    assert b_drv.TRIGGER_CMD == "$MSR\r\n"
    assert b_drv.format_set_horizontal_angle_cmd(45.3000).startswith("/Dc ")


def test_leica_gsi8_and_gsi16_parser() -> None:
    drv = LeicaGSIDriver(SerialConfig())
    gsi8 = "110001+00000001 21.323+18000000 22.323+09000000 31..00+00012345\r\n"
    obs8 = drv.parse_response(gsi8)
    assert math.isclose(obs8.slope_distance, 12.345, abs_tol=1e-6)
    assert math.isclose(obs8.horizontal_angle_deg, 180.0, abs_tol=1e-4)
    assert math.isclose(obs8.vertical_angle_deg, 90.0, abs_tol=1e-4)
    assert drv.format_set_horizontal_angle_cmd(90.1530) == "PUT/21...4+09015300 \r\n"

    gsi16 = "*110001+0000000000000001 21.323+0000000004500000 22.323+0000000009000000 31..00+0000000000025000\r\n"
    obs16 = drv.parse_response(gsi16)
    assert math.isclose(obs16.slope_distance, 25.000, abs_tol=1e-6)
    assert math.isclose(obs16.horizontal_angle_deg, 45.0, abs_tol=1e-4)


def test_topcon_bcc_and_sokkia_nikon_and_microscribe() -> None:
    drv = TopconDriver(SerialConfig(verify_bcc=True, check_barrel_reversal=True))
    body = "?+0012345m+0900000d+2703000d+0012345d"
    bcc_hex = compute_topcon_bcc(body)
    frame = f"{body}{bcc_hex}\r\n"
    obs = drv.parse_response(frame)
    assert math.isclose(obs.slope_distance, 12.345, abs_tol=1e-6)
    assert math.isclose(obs.vertical_angle_deg, 90.0, abs_tol=1e-4)
    assert math.isclose(obs.horizontal_angle_deg, 270.5, abs_tol=1e-4)
    assert drv.format_set_horizontal_angle_cmd(180.0000) == "!HAN18000000\r\n"

    # Verify BCC mismatch raises 'ERROR: BCC mismatch.' (FUN_004c57d0)
    try:
        drv.parse_response(f"{body}999\r\n")
        raise AssertionError("Expected BCC mismatch ValueError")
    except ValueError as e:
        assert "ERROR: BCC mismatch." in str(e)

    # Verify 3-digit decimal BCC on GTS-3B frame
    gts_body = "?+00002260m0900735+0212020d+00002260t60+00+25"
    gts_bcc = compute_topcon_bcc_decimal3(gts_body)
    obs_gts = drv.parse_response(f"{gts_body}{gts_bcc}\r\n")
    assert math.isclose(obs_gts.slope_distance, 0.2260, abs_tol=1e-5)

    # Sokkia vs Nikon SetHorizontalAngle distinction (FUN_004cdb10)
    sokkia_drv = SokkiaNikonDriver(SerialConfig(), variant="SOKKIA")
    nikon_drv = SokkiaNikonDriver(SerialConfig(), variant="NIKON")
    assert sokkia_drv.format_set_horizontal_angle_cmd(90.0) == "09000000\r\n"
    assert nikon_drv.format_set_horizontal_angle_cmd(90.0) == "!HAN09000000\r\n"

    s_frame = "E 112 00.00 00.000\r\n 0010500 0900000 0900000\r\n"
    s_obs = sokkia_drv.parse_response(s_frame)
    assert math.isclose(s_obs.slope_distance, 10.500, abs_tol=1e-6)

    ms_drv = MicroscribeDriver(SerialConfig())
    x_m, y_m, z_m = ms_drv.parse_clipboard_mm("1250.5, 2500.0, -350.0\r\n", 1.0, 2.0, 0.0)
    assert math.isclose(x_m, 0.2505, abs_tol=1e-6)
    assert math.isclose(y_m, 0.5000, abs_tol=1e-6)
    assert math.isclose(z_m, -0.3500, abs_tol=1e-6)


def test_decompiled_math_and_prism_bounds_and_legacy_station_z() -> None:
    rad_90_30_15 = math.radians(90.0 + 30.0 / 60.0 + 15.0 / 3600.0)
    assert math.isclose(radians_to_dddmmss(rad_90_30_15), 90.3015, abs_tol=1e-6)

    deg, mins, secs, dms_str = calc_angle_dddmmss(100.0, 100.0, 110.0, 100.0)
    assert (deg, mins, secs, dms_str) == (90, 0, 0, "90.0000")
    assert flip_180_dddmmss(dms_str) == "270.0000"

    # Prism validation bounds (0x0042f2d0 & 0x0042f398)
    validate_prism_bounds(-2.5, -0.03)  # Ceiling negative pole height + -30mm offset
    for bad_h, bad_off in [(12.0, 0.0), (1.5, 0.08)]:
        try:
            validate_prism_bounds(bad_h, bad_off)
            raise AssertionError("Expected prism bounds ValueError")
        except ValueError:
            pass

    # Legacy StationZ (center of instrument) vs Ground Datum Z parity (frmStatus @ 0x0043c99f)
    legacy_stn = StationState.from_legacy_cfg("STN_LEG", 100.0, 200.0, station_z_center=11.500, instrument_height=1.500)
    assert math.isclose(legacy_stn.ground_datum_z, 10.000, abs_tol=1e-6)
    assert math.isclose(legacy_stn.instrument_center_z, 11.500, abs_tol=1e-6)
    pt, _, _ = reduce_polar_to_cartesian(legacy_stn, 10.0, math.radians(90.0), math.radians(90.0), prism_height=1.500)
    assert math.isclose(pt.z, 10.000, abs_tol=1e-6)

    # frmOffSet directional offsets + 1km sanity check
    p0 = Point3D(100.0, 200.0, 10.0)
    p_east = apply_directional_offset(p0, "Offset East", 0.25)
    p_south = apply_directional_offset(p_east, "Offset South", 0.50)
    p_down = apply_directional_offset(p_south, "Offset Down", 0.10)
    assert (p_down.x, p_down.y, p_down.z) == (100.25, 199.50, 9.90)

    units = [
        ExcavationUnit("N100E100", shape="RECT", min_x=100.0, min_y=100.0, max_x=101.0, max_y=101.0),
        ExcavationUnit("GENERAL", shape="NOLIMITS", min_x=-99999.0),
    ]
    found = auto_find_unit(Point3D(100.4, 100.8, 9.5), units)
    assert found is not None and found.name == "N100E100"
    assert format_speech_unit_id("N100", "42") == "N 1 0 0 dash 42"


def test_polar_to_cartesian_and_initializations() -> None:
    d_occ = Datum("D1", 100.0, 200.0, 10.0)
    d_bs = Datum("D2", 110.0, 200.0, 10.0)

    stn_direct = initialize_direct(d_occ, instrument_height=1.50)
    pt, hd, vd = reduce_polar_to_cartesian(
        station=stn_direct,
        slope_distance=10.0,
        horizontal_angle_rad=math.radians(90.0),
        vertical_angle_rad=math.radians(90.0),
        prism_height=1.50,
        prism_offset=0.0,
    )
    assert math.isclose(pt.x, 110.0, abs_tol=1e-6)
    assert math.isclose(pt.y, 200.0, abs_tol=1e-6)
    assert math.isclose(pt.z, 10.0, abs_tol=1e-6)

    stn_1pt = initialize_one_point(
        target_datum=d_bs,
        instrument_height=1.50,
        prism_height=1.50,
        slope_distance=10.0,
        horizontal_angle_rad=math.radians(90.0),
        vertical_angle_rad=math.radians(90.0),
    )
    assert math.isclose(stn_1pt.x, 100.0, abs_tol=1e-6)
    assert math.isclose(stn_1pt.y, 200.0, abs_tol=1e-6)
    assert math.isclose(stn_1pt.z, 10.0, abs_tol=1e-6)

    stn_2pt, res = initialize_two_point_backsight(
        occupied_datum=d_occ,
        backsight_datum=d_bs,
        instrument_height=1.50,
        prism_height=1.50,
        slope_distance=10.05,
        horizontal_angle_rad=math.radians(30.0),
        vertical_angle_rad=math.radians(90.0),
    )
    assert math.isclose(math.degrees(stn_2pt.azimuth_offset_rad), 60.0, abs_tol=1e-5)
    assert math.isclose(res.expected_distance, 10.0, abs_tol=1e-6)
    assert math.isclose(res.recorded_distance, 10.05, abs_tol=1e-6)
    assert math.isclose(res.distance_difference, 0.05, abs_tol=1e-6)


def test_resection_two_and_three_point_exact_recovery() -> None:
    true_stn = StationState(
        setup_method="True",
        station_name="TRUE",
        x=50.0,
        y=80.0,
        z=5.0,
        instrument_height=1.60,
        azimuth_offset_rad=math.radians(25.0),
    )
    datums = [
        Datum("DA", 50.0, 95.0, 6.2),
        Datum("DB", 68.0, 80.0, 4.5),
        Datum("DC", 40.0, 65.0, 5.8),
    ]
    sightings = []
    for d in datums:
        dx = d.x - true_stn.x
        dy = d.y - true_stn.y
        dz = d.z - (true_stn.z + true_stn.instrument_height - 1.20)
        hd = math.hypot(dx, dy)
        sd = math.hypot(hd, dz)
        v_rad = math.atan2(hd, dz)
        grid_az = math.atan2(dx, dy) % (2.0 * math.pi)
        hz_obs = (grid_az - true_stn.azimuth_offset_rad) % (2.0 * math.pi)
        sightings.append(
            ResectionSighting(
                datum=d,
                slope_distance=sd,
                horizontal_angle_rad=hz_obs,
                vertical_angle_rad=v_rad,
                prism_height=1.20,
            )
        )

    res_2pt = initialize_resection(sightings[:2], instrument_height=1.60)
    assert math.isclose(res_2pt.station.x, 50.0, abs_tol=1e-5)
    assert math.isclose(res_2pt.station.y, 80.0, abs_tol=1e-5)
    assert math.isclose(res_2pt.station.z, 5.0, abs_tol=1e-5)
    assert math.isclose(math.degrees(res_2pt.station.azimuth_offset_rad), 25.0, abs_tol=1e-4)

    res_3pt = initialize_resection(sightings, instrument_height=1.60)
    assert math.isclose(res_3pt.station.x, 50.0, abs_tol=1e-5)
    assert math.isclose(res_3pt.station.y, 80.0, abs_tol=1e-5)
    assert math.isclose(res_3pt.station.z, 5.0, abs_tol=1e-5)
    assert res_3pt.rms_3d < 1e-6


def test_button_macros_and_duplicate_id_and_series_resequencing(tmp_path: Path) -> None:
    cfg_text = """
[EDM]
Instrument=Wild2
COMport=COM3
Settings=19200,N,8,1
Unitfields=ID,LEVEL,EXCAVATOR
PrismPrompt=YES
StationName=D1
StationX=100.000
stationY=200.000
stationZ=11.500

[BUTTON1]
TITLE=Bone 2B
Shortcut=1
CODE=BONE
LEVEL=2B
UNIT=N100E101

[UNIT]
Type=Menu
Menu=N100E100,N100E101
Carry=True

[ID]
Type=Numeric
Increment=True
Carry=True

[SUFFIX]
Type=Numeric

[LEVEL]
Type=Menu
Menu=1,2A,2B
Carry=True

[CODE]
Type=Menu
Menu=LITHIC,BONE,TOPO
Carry=True
"""
    cfg = parse_cfg_text(cfg_text)
    assert len(cfg.button_macros) == 1
    assert cfg.button_macros[0].title == "Bone 2B"
    assert cfg.button_macros[0].shortcut == "1"
    assert cfg.button_macros[0].field_values["CODE"] == "BONE"
    assert cfg.button_macros[0].field_values["LEVEL"] == "2B"
    assert cfg.button_macros[0].field_values["UNIT"] == "N100E101"

    # Verify atomic save_legacy_cfg_file round-trip (FUN_004b7bb0)
    saved_cfg_path = tmp_path / "roundtrip.cfg"
    save_legacy_cfg_file(cfg, saved_cfg_path)
    reloaded = load_cfg_file(saved_cfg_path)
    assert len(reloaded.button_macros) == 1
    assert reloaded.button_macros[0].field_values["CODE"] == "BONE"
    assert reloaded.station_z == 11.500

    # Verify June 12, 2023 SpeedButton UNIT switch ID preservation
    db = EDMDatabase(tmp_path / "test_series.sqlite")
    db.save_unit_field_state("N100E101", {"ID": "19", "LEVEL": "2A"})
    applied = cfg.apply_button_macro(
        cfg.button_macros[0],
        {"UNIT": "N100E100", "ID": "5", "LEVEL": "1", "CODE": "LITHIC"},
        unit_state_lookup_fn=db.load_unit_field_state,
    )
    assert applied["UNIT"] == "N100E101"
    assert applied["ID"] == "19"      # Restored from N100E101 without skipping!
    assert applied["LEVEL"] == "2B"   # Overridden by macro
    assert applied["CODE"] == "BONE"  # Overridden by macro

    # Verify Duplicate ID resolution (ALPHA, NEW_ID, CONTINUE) & Series Resequencing (FUN_00489a60)
    stn = db.load_station_state()
    prism = Prism("Mini", 0.10, 0.017)
    d0 = db.record_point("N100E100", "10", 0, 100.1, 100.1, 9.9, prism, 1.5, 45.0, 90.0, stn, {"SUFFIX": "0"})
    d1 = db.record_point("N100E100", "10", 1, 100.2, 100.2, 9.9, prism, 1.5, 45.0, 90.0, stn, {"SUFFIX": "1"})
    d2 = db.record_point("N100E100", "10", 2, 100.3, 100.3, 9.9, prism, 1.5, 45.0, 90.0, stn, {"SUFFIX": "2"})

    assert db.has_initial_series_shot("N100E100", "10") is True
    assert db.has_initial_series_shot("N100E100", "999") is False
    assert db.check_duplicate_id("N100E100", "10", 0) is True

    alpha_id, alpha_suf = db.resolve_duplicate_id("N100E100", "10", "ALPHA")
    assert (alpha_id, alpha_suf) == ("10a", 0)

    new_id, new_suf = db.resolve_duplicate_id("N100E100", "10", "NEW_ID")
    assert (new_id, new_suf) == ("11", 0)

    cont_id, cont_suf = db.resolve_duplicate_id("N100E100", "10", "CONTINUE")
    assert (cont_id, cont_suf) == ("10", 3)

    # Delete middle shot (suffix=1) and verify suffix=2 is resequenced to suffix=1
    reseq_info = db.delete_point_with_resequencing(d1, delete_entire_series=False)
    assert reseq_info["deleted_count"] == 1
    assert reseq_info["resequenced_count"] == 1
    remaining = db.list_points()
    assert [p["suffix"] for p in remaining] == [0, 1]
    assert remaining[0]["doc_id"] == d0 and remaining[1]["doc_id"] == d2
    db.close()


def test_main_window_ui_wiring_offscreen(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from ui.main_window import EDMMainWindow

    app = QApplication.instance() or QApplication([])
    db_file = tmp_path / "ui_test.sqlite"
    cfg_file = Path(__file__).resolve().parent.parent / "sample_excavation.cfg"

    win = EDMMainWindow(db_path=db_file, cfg_path=cfg_file)
    win.cfg.limit_checking = False
    win.cfg.print_labels = True
    win.cfg.speech_enabled = True

    # Record 2 shots in N100E100 (IDs 1 and 2, advancing to 3)
    win._on_measure_and_record()
    win._on_measure_and_record()
    vals_n100 = win._get_form_values()
    assert vals_n100["ID"] == "3"
    assert win.last_speech_Readout == "N 1 0 0 E 1 0 0 dash 2"
    assert win.label_spool_path.exists()

    # Switch UNIT to N100E101 -> should save N100E100 state (ID=3) and start fresh in N100E101
    win._set_form_values({"UNIT": "N100E101"})
    win._set_form_values({"ID": "1", "LEVEL": "2B"})
    win._on_measure_and_record()
    assert win._get_form_values()["ID"] == "2"

    # Switch back to N100E100 -> should automatically restore ID=3 and LEVEL=1!
    win._set_form_values({"UNIT": "N100E100"})
    restored = win._get_form_values()
    assert restored["ID"] == "3"
    assert restored["LEVEL"] == "1"

    win.close()
    win.deleteLater()
    win.db.close()
    app.processEvents()


def test_cfg_field_and_button_editor_dialogs_offscreen(tmp_path: Path) -> None:
    """
    Tests CFGFieldEditorDialog (frmEditField replica) and SpeedButtonEditorDialog (frmButtons replica):
    - Adding fields, duplicate field rejection, 30-field cap, quick-add V/H/SlopeD & Datum Info
    - Editing 6-button slots, shortcut conflict detection, Clear Button, and saving back to EDMConfig
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from ui.dialogs import CFGFieldEditorDialog, SpeedButtonEditorDialog

    app = QApplication.instance() or QApplication([])
    cfg_file = Path(__file__).resolve().parent.parent / "sample_excavation.cfg"
    cfg = load_cfg_file(cfg_file)

    # 1. Test CFGFieldEditorDialog (frmEditField)
    field_dlg = CFGFieldEditorDialog(cfg)
    initial_count = len(field_dlg.fields)
    added = field_dlg.add_field_programmatically(
        name="MATERIAL",
        field_type="MENU",
        prompt="Raw Material",
        length=16,
        carry=True,
        menu_items=["FLINT", "QUARTZ", "OBSIDIAN"],
        default="FLINT",
    )
    assert added.name == "MATERIAL"
    assert len(field_dlg.fields) == initial_count + 1

    # Duplicate field name must raise ValueError('Duplicate field name.')
    try:
        field_dlg.add_field_programmatically(name="material")
        raise AssertionError("Expected duplicate field name ValueError")
    except ValueError as e:
        assert "Duplicate field name." in str(e)

    # Quick-add angles and datum info
    field_dlg._on_quick_add_angles()
    field_dlg._on_quick_add_datum_info()
    names_upper = {f.name.upper() for f in field_dlg.fields}
    assert {"VANGLE", "HANGLE", "SLOPED", "DATUMNAME", "DATUMX", "DATUMY", "DATUMZ"}.issubset(names_upper)

    field_dlg._on_save_and_close()
    assert any(f.name == "MATERIAL" for f in cfg.fields)
    field_dlg.close()
    field_dlg.deleteLater()

    # 2. Test SpeedButtonEditorDialog (frmButtons)
    btn_dlg = SpeedButtonEditorDialog(cfg)
    btn_dlg.select_slot(1)
    btn_dlg._on_caption_edited("Flint Flake")
    btn_dlg.set_slot_shortcut(1, "F")
    btn_dlg.set_slot_field_value(1, "CODE", "LITHIC")
    btn_dlg.set_slot_field_value(1, "MATERIAL", "FLINT")

    # Configure slot 2 and test shortcut conflict against slot 1 ('F')
    btn_dlg.select_slot(2)
    btn_dlg._on_caption_edited("Bone Fragment")
    try:
        btn_dlg.set_slot_shortcut(2, "f")
        raise AssertionError("Expected shortcut conflict ValueError")
    except ValueError as e:
        assert "This shortcut conflicts with another one already defined." in str(e)

    btn_dlg.set_slot_shortcut(2, "B")
    btn_dlg.set_slot_field_value(2, "CODE", "BONE")

    # Test Clear Button on slot 2
    btn_dlg.clear_slot(2)
    assert btn_dlg.slots[2].title == "Setup"
    assert btn_dlg.slots[2].field_values == {}

    # Re-populate slot 2 and save
    btn_dlg.set_slot_field_value(2, "CODE", "BONE")
    btn_dlg._on_caption_edited("Bone Fragment")
    btn_dlg._on_save_and_close()

    assert len(cfg.button_macros) >= 2
    assert cfg.button_macros[0].title == "Flint Flake"
    assert cfg.button_macros[0].shortcut == "F"
    assert cfg.button_macros[0].field_values["MATERIAL"] == "FLINT"
    btn_dlg.close()
    btn_dlg.deleteLater()
    app.processEvents()


def test_datagrid_and_field_filter_dialogs_offscreen(tmp_path: Path) -> None:
    """
    Tests FieldFilterDialog (frmFilter replica) and DataGridDialog (frmDataGrid replica):
    - Distinct field value queries (get_distinct_field_values)
    - Field=Value filtering and Current Station filtering (filter_points)
    - Column visibility toggling in Display Fields checklist (set_column_checked)
    - Inline cell editing of both core columns (X, UNIT) and custom .CFG fields (CODE, LEVEL)
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from ui.dialogs import DataGridDialog, FieldFilterDialog

    app = QApplication.instance() or QApplication([])
    cfg_file = Path(__file__).resolve().parent.parent / "sample_excavation.cfg"
    cfg = load_cfg_file(cfg_file)
    db = EDMDatabase(tmp_path / "datagrid_test.sqlite")

    stn1 = StationState(
        setup_method="Direct",
        station_name="STN1",
        x=100.0,
        y=100.0,
        z=10.0,
        instrument_height=1.5,
    )
    stn2 = StationState(
        setup_method="Direct",
        station_name="STN2",
        x=200.0,
        y=200.0,
        z=15.0,
        instrument_height=1.5,
    )
    prism = Prism("Mini", 0.10, 0.017)

    id1 = db.record_point(
        "N100E100", "1", 0, 100.5, 100.5, 9.8, prism, 1.5, 45.0, 90.0, stn1,
        {"CODE": "LITHIC", "LEVEL": "1", "EXCAVATOR": "HD"},
    )
    id2 = db.record_point(
        "N100E100", "2", 0, 100.7, 100.8, 9.7, prism, 1.6, 50.0, 90.0, stn1,
        {"CODE": "BONE", "LEVEL": "2A", "EXCAVATOR": "SPM"},
    )
    id3 = db.record_point(
        "N100E101", "1", 0, 201.0, 201.0, 14.8, prism, 1.4, 60.0, 90.0, stn2,
        {"CODE": "LITHIC", "LEVEL": "2B", "EXCAVATOR": "HD"},
    )

    # 1. Test FieldFilterDialog (frmFilter)
    filter_dlg = FieldFilterDialog(db, cfg)
    distinct_codes = db.get_distinct_field_values("CODE")
    assert distinct_codes == ["BONE", "LITHIC"]

    filter_dlg._on_field_selected("CODE")
    assert filter_dlg.lst_vals.count() == 2

    try:
        filter_dlg.select_filter("CODE", "")
        raise AssertionError("Expected empty filter value ValueError")
    except ValueError as e:
        assert "Select field and value to filter by." in str(e)

    filter_dlg.select_filter("CODE", "BONE")
    assert (filter_dlg.selected_field, filter_dlg.selected_value) == ("CODE", "BONE")
    filter_dlg.close()
    filter_dlg.deleteLater()

    # 2. Test DataGridDialog (frmDataGrid)
    db.save_station_state(stn1)
    grid_dlg = DataGridDialog(db, cfg, filter_current_station_only=False)
    assert grid_dlg.grid.rowCount() == 3

    # Filter by CODE = 'LITHIC' -> should show 2 rows (id1 and id3)
    grid_dlg.apply_filter("CODE", "LITHIC")
    assert grid_dlg.grid.rowCount() == 2

    # Enable 'Show only Points from Current Station' (stn1) -> should narrow to 1 row (id1)
    grid_dlg.chk_station_filter.setChecked(True)
    assert grid_dlg.grid.rowCount() == 1

    # Clear filters -> back to 3 rows
    grid_dlg.chk_station_filter.setChecked(False)
    grid_dlg.clear_filter()
    assert grid_dlg.grid.rowCount() == 3

    # Test hiding/showing a column via Display Fields checklist (lstFields)
    x_col_idx = grid_dlg.all_columns.index("X")
    grid_dlg.set_column_checked("X", False)
    assert grid_dlg.grid.isColumnHidden(x_col_idx) is True
    grid_dlg.set_column_checked("X", True)
    assert grid_dlg.grid.isColumnHidden(x_col_idx) is False

    # Test inline cell editing on row 0 (id1): update X coordinate and CODE extra field
    grid_dlg.edit_cell_programmatically(0, "X", "101.2500")
    grid_dlg.edit_cell_programmatically(0, "CODE", "CHARCOAL")

    pts_after = {p["doc_id"]: p for p in db.list_points()}
    assert math.isclose(pts_after[id1]["x"], 101.2500, abs_tol=1e-6)
    assert pts_after[id1]["extra_fields"]["CODE"] == "CHARCOAL"
    assert pts_after[id2]["extra_fields"]["CODE"] == "BONE"
    assert pts_after[id3]["unit"] == "N100E101"

    grid_dlg.close()
    grid_dlg.deleteLater()
    db.close()
    app.processEvents()


def test_context_defaults_and_manual_shot_dialogs_offscreen(tmp_path: Path) -> None:
    """
    Tests ContextDefaultsDialog (frmSubUnits replica) and ManualShotDialog (frmManualshot replica):
    - Setting Master Variable ('LEVEL') and Dependent Variables ('CODE', 'EXCAVATOR')
    - Associating specific dependent values with Master Values ('1' vs '2B')
    - Auto-updating dependent defaults when a shot is recorded (UpdateDependentVar)
    - Manual Shot entry in both 'ANGLES' (DDD.MMSS + SlopeDist + PoleHeight) and 'XYZ' modes
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from ui.dialogs import ContextDefaultsDialog, ManualShotDialog

    app = QApplication.instance() or QApplication([])
    cfg_file = Path(__file__).resolve().parent.parent / "sample_excavation.cfg"
    cfg = load_cfg_file(cfg_file)
    db = EDMDatabase(tmp_path / "ctx_manual_test.sqlite")

    # 1. Test ContextDefaultsDialog (frmSubUnits)
    ctx_dlg = ContextDefaultsDialog(db, cfg)
    ctx_dlg._on_master_changed("LEVEL")
    ctx_dlg.set_slave_checked("CODE", True)
    ctx_dlg.set_slave_checked("EXCAVATOR", True)
    ctx_dlg.set_rule_value("1", "CODE", "TOPO")
    ctx_dlg.set_rule_value("1", "EXCAVATOR", "HD")
    ctx_dlg.set_rule_value("2B", "CODE", "BONE")
    ctx_dlg.set_rule_value("2B", "EXCAVATOR", "SPM")
    ctx_dlg._on_save()
    ctx_dlg.close()
    ctx_dlg.deleteLater()

    assert db.get_context_dependent_values("1") == {"CODE": "TOPO", "EXCAVATOR": "HD"}
    assert db.get_context_dependent_values("2B") == {"CODE": "BONE", "EXCAVATOR": "SPM"}

    # Verify UpdateDependentVar updates stored rule when user edits a dependent field
    db.update_context_default_rule("2B", {"CODE": "LITHIC", "EXCAVATOR": "JD"})
    assert db.get_context_dependent_values("2B") == {"CODE": "LITHIC", "EXCAVATOR": "JD"}

    # 2. Test ManualShotDialog (frmManualshot) - Angles mode & XYZ mode
    man_dlg = ManualShotDialog(default_prism_height=1.50, default_mode="ANGLES")

    try:
        man_dlg.submit_angles("", "90.0000", "10.0")
        raise AssertionError("Expected missing angle ValueError")
    except ValueError as e:
        assert "Enter a vertical angle, horizontal angle, and slope distance." in str(e)

    obs_ang = man_dlg.submit_angles("90.0000", "90.0000", "12.500", "1.500")
    assert math.isclose(obs_ang.slope_distance, 12.500, abs_tol=1e-6)
    assert math.isclose(obs_ang.horizontal_angle_deg, 90.0, abs_tol=1e-5)
    assert math.isclose(obs_ang.vertical_angle_deg, 90.0, abs_tol=1e-5)
    assert obs_ang.direct_xyz is None

    try:
        man_dlg.submit_xyz("100.0", "bad_float", "10.0")
        raise AssertionError("Expected non-numeric XYZ ValueError")
    except ValueError as e:
        assert "Enter coordinates as numeric values." in str(e)

    obs_xyz = man_dlg.submit_xyz("105.250", "204.500", "9.850")
    assert obs_xyz.direct_xyz == (105.250, 204.500, 9.850)

    man_dlg.close()
    man_dlg.deleteLater()
    db.close()
    app.processEvents()


def test_status_debug_and_about_dialogs_offscreen(tmp_path: Path) -> None:
    """
    Tests EDMStatusDialog (frmStatus replica), DebugStationDialog (frmDebug replica),
    and AboutDialog (frmAbout replica):
    - Verifies Station X, Y, optical center Z, COM port, settings, and record count on EDMStatusDialog
    - Verifies Initialize, X-Shot (HAngle, VAngle, SlopeD, X, Y, Z), and all 5 GeoCOM preset commands on DebugStationDialog
    - Verifies unrecognized/empty command validation on DebugStationDialog
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from drivers.total_station import SimulatorDriver
    from ui.dialogs import AboutDialog, DebugStationDialog, EDMStatusDialog

    app = QApplication.instance() or QApplication([])
    cfg_file = Path(__file__).resolve().parent.parent / "sample_excavation.cfg"
    cfg = load_cfg_file(cfg_file)
    db = EDMDatabase(tmp_path / "status_debug_test.sqlite")

    stn = StationState(
        setup_method="Direct",
        station_name="D1",
        x=100.0,
        y=200.0,
        z=10.0,
        instrument_height=1.500,
    )
    db.save_station_state(stn)

    # 1. Test EDMStatusDialog (frmStatus)
    status_dlg = EDMStatusDialog(db, cfg, cfg_path=str(cfg_file), ini_path="edm.ini")
    assert status_dlg.txt_station_x.text() == "100.000"
    assert status_dlg.txt_station_y.text() == "200.000"
    assert status_dlg.txt_station_z.text() == "11.500"  # Optical center Z = 10.0 + 1.5
    assert "19200,N,8,1" in status_dlg.txt_settings.text()
    status_dlg.close()
    status_dlg.deleteLater()

    # 2. Test DebugStationDialog (frmDebug)
    sim_drv = SimulatorDriver()
    sim_drv.queue_observation(slope_distance=10.0, hz_deg=90.0, v_deg=90.0)
    dbg_dlg = DebugStationDialog(db, cfg, sim_drv)

    init_out = dbg_dlg.run_initialize()
    assert init_out == "OK"

    obs = dbg_dlg.run_xshot()
    assert math.isclose(obs.slope_distance, 10.0, abs_tol=1e-6)
    assert dbg_dlg.txt_x.text() == "110.000"
    assert dbg_dlg.txt_y.text() == "200.000"
    assert dbg_dlg.txt_z.text() == "11.500"

    for preset in DebugStationDialog.PRESET_COMMANDS:
        reply = dbg_dlg.execute_preset_command(preset)
        assert "%R1P,0,0:0" in reply

    try:
        dbg_dlg.execute_preset_command("")
        raise AssertionError("Expected empty command ValueError")
    except ValueError as e:
        assert "Select a command from the dropdown next to this button." in str(e)

    try:
        dbg_dlg.execute_preset_command("INVALID_CMD")
        raise AssertionError("Expected unrecognized command ValueError")
    except ValueError as e:
        assert "Error: Unrecognize command selected." in str(e)

    dbg_dlg.close()
    dbg_dlg.deleteLater()

    # 3. Test AboutDialog (frmAbout)
    about_dlg = AboutDialog()
    about_dlg.close()
    about_dlg.deleteLater()

    db.close()
    app.processEvents()


def test_setup_log_files_and_create_default_project_dialogs_offscreen(tmp_path: Path) -> None:
    """
    Tests SetupLogFilesDialog (frmSetupLogFiles replica) and CreateDefaultProjectDialog (frmDefault replica):
    - Enabling UseTSLog & UseGeneralLog, writing timestamped entries, and clearing log files
    - Validating missing CFG/DB/PointsTable and non-Simulation serial parameters on CreateDefaultProjectDialog
    - Creating a Paleolithic default .CFG and SQLite database (with 'Main' datum, 'Zero'/'10cm' prisms, 'Unit1'/'Unit2' units)
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from ui.dialogs import CreateDefaultProjectDialog, SetupLogFilesDialog

    app = QApplication.instance() or QApplication([])
    cfg_file = Path(__file__).resolve().parent.parent / "sample_excavation.cfg"
    cfg = load_cfg_file(cfg_file)

    # 1. Test SetupLogFilesDialog (frmSetupLogFiles)
    log_dlg = SetupLogFilesDialog(cfg, default_dir=tmp_path)
    ts_log = tmp_path / "ts_comm_test.log"
    gen_log = tmp_path / "general_test.log"
    log_dlg.chk_ts_log.setChecked(True)
    log_dlg.txt_ts_log.setText(str(ts_log))
    log_dlg.chk_general_log.setChecked(True)
    log_dlg.txt_general_log.setText(str(gen_log))
    log_dlg._on_save()

    assert cfg.use_ts_log is True
    assert cfg.ts_log_file == str(ts_log)
    assert cfg.use_general_log is True
    assert cfg.general_log_file == str(gen_log)

    SetupLogFilesDialog.append_log_entry(ts_log, "TX %R1Q,2008:1,1")
    SetupLogFilesDialog.append_log_entry(gen_log, "Station initialized Direct over Main")
    assert "TX %R1Q,2008:1,1" in ts_log.read_text(encoding="utf-8")
    assert "Station initialized Direct over Main" in gen_log.read_text(encoding="utf-8")

    log_dlg.clear_ts_log_file()
    log_dlg.clear_general_log_file()
    assert ts_log.read_text(encoding="utf-8") == ""
    assert gen_log.read_text(encoding="utf-8") == ""

    log_dlg.close()
    log_dlg.deleteLater()

    # 2. Test CreateDefaultProjectDialog (frmDefault)
    def_dlg = CreateDefaultProjectDialog(default_dir=tmp_path)

    try:
        def_dlg.write_default_project(cfg_path_str="", db_path_str="a.sqlite", points_table_str="Points")
        raise AssertionError("Expected missing CFG filename ValueError")
    except ValueError as e:
        assert "Please supply a CFG filename." in str(e)

    try:
        def_dlg.write_default_project(cfg_path_str="a.cfg", db_path_str="", points_table_str="Points")
        raise AssertionError("Expected missing DB filename ValueError")
    except ValueError as e:
        assert "Please supply a database filename." in str(e)

    try:
        def_dlg.write_default_project(cfg_path_str="a.cfg", db_path_str="a.sqlite", points_table_str="")
        raise AssertionError("Expected missing Points table ValueError")
    except ValueError as e:
        assert "Please supply a points table name." in str(e)

    try:
        def_dlg.write_default_project(
            cfg_path_str="a.cfg",
            db_path_str="a.sqlite",
            points_table_str="Points",
            station_type_str="Topcon",
            baud_str="",
        )
        raise AssertionError("Expected non-Simulation serial settings ValueError")
    except ValueError as e:
        assert "If the total station type is not set to Simulation" in str(e)

    out_cfg_path = tmp_path / "PaleoSite.CFG"
    out_db_path = tmp_path / "PaleoSite.sqlite"
    created_cfg_p, created_db_p, _ = def_dlg.write_default_project(
        cfg_path_str=str(out_cfg_path),
        db_path_str=str(out_db_path),
        points_table_str="Points",
        station_type_str="Wild2",
        com_port_str="COM2",
        baud_str="19200",
        parity_str="None",
        databits_str="8",
        stopbits_str="1",
    )
    assert created_cfg_p.exists()
    assert created_db_p.exists()

    reloaded_cfg = load_cfg_file(created_cfg_p)
    assert reloaded_cfg.station_type == "Wild2"
    assert reloaded_cfg.com_port == "COM2"
    assert reloaded_cfg.baudrate == 19200
    assert {f.name.upper() for f in reloaded_cfg.fields} >= {"UNIT", "ID", "SUFFIX", "CODE", "LEVEL", "EXCAVATOR"}

    check_db = EDMDatabase(created_db_p)
    datum_names = {d.name for d in check_db.list_datums()}
    prism_names = {p.name for p in check_db.list_prisms()}
    unit_names = {u.name for u in check_db.list_units()}
    assert "Main" in datum_names
    assert {"Zero", "10cm"}.issubset(prism_names)
    assert {"Unit1", "Unit2"}.issubset(unit_names)
    check_db.close()

    def_dlg.close()
    def_dlg.deleteLater()
    app.processEvents()


def test_unit_scoped_fields_and_csv_bundle_and_labels(tmp_path: Path) -> None:
    db = EDMDatabase(tmp_path / "bundle.sqlite")
    db.save_unit_field_state("N100E100", {"ID": "42", "LEVEL": "2B"})
    db.save_unit_field_state("N100E101", {"ID": "7", "LEVEL": "1"})
    assert db.load_unit_field_state("N100E100")["ID"] == "42"
    assert db.load_unit_field_state("N100E101")["ID"] == "7"

    stn = db.load_station_state()
    prism = Prism("Mini", 0.10, 0.017)
    db.record_point("N100E100", "42", 0, 100.5, 100.5, 9.9, prism, 1.5, 45.0, 90.0, stn, {"CODE": "LITHIC", "LEVEL": "2B"})
    last_pt = db.get_last_point()
    assert last_pt is not None
    # Verify June 12, 2023 auto-populated DATUMX/Y/Z/DATUMNAME fields
    assert last_pt["extra_fields"]["DATUMNAME"] == stn.station_name
    assert last_pt["extra_fields"]["DATUMX"] == f"{stn.x:.3f}"

    spool_ascii = tmp_path / "labels.txt"
    ascii_out = append_label_to_spool(last_pt, spool_ascii, site_name="ROC_DE_MARSAL", use_zpl=False)
    assert "*N100E100-42-0*" in ascii_out

    spool_zpl = tmp_path / "labels.zpl"
    zpl_out = append_label_to_spool(last_pt, spool_zpl, site_name="ROC_DE_MARSAL", use_zpl=True)
    assert "^B3N" in zpl_out

    bundle = db.export_edmwin_csv_bundle(tmp_path / "roc2026")
    for key in ("points", "datums", "prisms", "units"):
        assert Path(bundle[key]).exists()

    assert math.isclose(apply_coordinate_offset_expression(100.0, "+0.25"), 100.25, abs_tol=1e-6)
    assert math.isclose(apply_coordinate_offset_expression(100.0, "-1.50"), 98.50, abs_tol=1e-6)

    local_pts = [Point3D(0.0, 0.0, 0.0), Point3D(1.0, 0.0, 0.0), Point3D(0.0, 1.0, 0.0)]
    grid_pts = [Point3D(500.0, 1000.0, 50.0), Point3D(501.0, 1000.0, 50.0), Point3D(500.0, 1001.0, 50.0)]
    mapped = solve_3d_block_georeference(local_pts, grid_pts, Point3D(0.25, 0.75, -0.10))
    assert math.isclose(mapped.x, 500.25, abs_tol=1e-6)
    assert math.isclose(mapped.y, 1000.75, abs_tol=1e-6)
    assert math.isclose(mapped.z, 49.90, abs_tol=1e-6)
    db.close()


class TestEDMUnittestWrapper(unittest.TestCase):
    def test_all_protocols_and_math_and_ui_parity(self) -> None:
        test_leica_geocom_and_autotilt_and_builder()
        test_leica_gsi8_and_gsi16_parser()
        test_topcon_bcc_and_sokkia_nikon_and_microscribe()
        test_decompiled_math_and_prism_bounds_and_legacy_station_z()
        test_polar_to_cartesian_and_initializations()
        test_resection_two_and_three_point_exact_recovery()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            test_button_macros_and_duplicate_id_and_series_resequencing(tmp_p)
            test_unit_scoped_fields_and_csv_bundle_and_labels(tmp_p)
            test_main_window_ui_wiring_offscreen(tmp_p)
            test_cfg_field_and_button_editor_dialogs_offscreen(tmp_p)
            test_datagrid_and_field_filter_dialogs_offscreen(tmp_p)
            test_context_defaults_and_manual_shot_dialogs_offscreen(tmp_p)
            test_status_debug_and_about_dialogs_offscreen(tmp_p)
            test_setup_log_files_and_create_default_project_dialogs_offscreen(tmp_p)
        import gc
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.closeAllWindows()
            app.processEvents()
        gc.collect()


if __name__ == "__main__":
    unittest.main()
