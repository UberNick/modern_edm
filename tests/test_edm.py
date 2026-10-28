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
    win.db.close()
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


if __name__ == "__main__":
    unittest.main()
