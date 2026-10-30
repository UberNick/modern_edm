"""
Surveying & Geodetic Math Engine for EDMWin & EDMpy (surf3-edm) Replacement.
Includes exact replicas of all decompiled edmwin.exe & surf3-edm math routines:
- Polar-to-XYZ reduction (FUN_004c8840 / FUN_004cee60 / vhd_to_xyz)
- Legacy StationZ (center-of-instrument) vs. Ground Datum elevation handling (frmStatus @ 0x0043c99f)
- Prism Height [-10, 10]m & Offset [-0.05, 0.05]m validation (frmPolesheet @ 0x0042f2d0 / 0x0042f398)
- CalcAngle 2-point azimuth in DDD.MMSS & (Deg, Min, Sec) (FUN_004b5fb0 / angle_between_xy_pairs)
- Flip 180 Degrees (calccomp on frmStationSetup)
- Initialize Direct, 1-Point, 2-Point Backsight (with optional Station Z solve & 3D distance error),
  Carsac 2-Shot Setup (solve_two_shot_setup), and 2/3+ Point Resection (unweighted & weighted 2x2 SVD compute_free_station)
- 3-Datum 3D Rodrigues Triangle-Leg + Surface-Normal Alignment (rotate_point / solve_3d_block_georeference)
- Multi-Datum 7-Parameter 3D Helmert Transformation (compute_helmert_transformation / helmert_3d_transform)
- Safe Inline Coordinate Arithmetic Evaluator (evaluate_coordinate_math / do_coordinate_math)
- Verify Station (frmStationVerify)
- 6-Direction Post-Shot Coordinate Offset (frmOffSet: East, West, North, South, Up, Down)
- Auto-Find Unit (FindUnit SQL bounding-box & circle lookup + unbounded minx=-99999 units)
- SAPI Voice Readout Formatter (FUN_004c2b70: "<Unit> dash <ID>")
"""

from __future__ import annotations

import ast
import math
import operator as _op
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Point3D:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class Datum:
    name: str
    x: float
    y: float
    z: float
    description: str = ""


@dataclass(frozen=True)
class Prism:
    name: str
    height: float
    offset: float = 0.0
    description: str = ""


def validate_prism_bounds(height: float, offset: float) -> None:
    """
    Replicates frmPolesheet validation in edmwin.exe (0x0042f2d0 & 0x0042f398):
    - Prism heights must be between -10 and 10 meters (negative numbers for cave ceilings).
    - Prism offsets must be between -0.05 and 0.05 meters.
    """
    if not (-10.0 <= height <= 10.0):
        raise ValueError(
            "Prism heights must be between -10 and 10 meters (tip: use negative numbers for measuring ceilings"
        )
    if not (-0.05 <= offset <= 0.05):
        raise ValueError(
            "Prism offsets must be between -0.05 and 0.05 meters"
        )


@dataclass(frozen=True)
class ExcavationUnit:
    name: str
    shape: str = "RECT"  # 'RECT', 'CIRCLE', or 'NOLIMITS' (min_x = -99999)
    min_x: float = 0.0
    min_y: float = 0.0
    max_x: float = 0.0
    max_y: float = 0.0
    center_x: float = 0.0
    center_y: float = 0.0
    radius: float = 0.0
    min_z: Optional[float] = None
    max_z: Optional[float] = None


@dataclass
class StationState:
    setup_method: str = "Direct"
    station_name: str = "STN1"
    x: float = 100.0
    y: float = 100.0
    z: float = 10.0
    instrument_height: float = 1.500
    azimuth_offset_rad: float = 0.0
    rms_error_2d: float = 0.0
    rms_error_z: float = 0.0
    z_is_instrument_center: bool = False

    @property
    def instrument_center_z(self) -> float:
        """
        Returns the Z coordinate at the optical center of the total station
        (matching legacy edmwin.exe StationZ in EDM.INI / frmStatus @ 0x0043c99f).
        """
        return self.z if self.z_is_instrument_center else (self.z + self.instrument_height)

    @property
    def ground_datum_z(self) -> float:
        """Returns the ground datum Z coordinate beneath the station."""
        return (self.z - self.instrument_height) if self.z_is_instrument_center else self.z

    @classmethod
    def from_legacy_cfg(
        cls,
        station_name: str,
        station_x: float,
        station_y: float,
        station_z_center: float,
        instrument_height: float = 0.0,
        setup_method: str = "Direct",
    ) -> "StationState":
        """
        Constructs a StationState from legacy edmwin.exe CFG / INI values where
        StationZ is already the optical center of the total station.
        """
        return cls(
            setup_method=setup_method,
            station_name=station_name or "STN1",
            x=station_x,
            y=station_y,
            z=station_z_center - instrument_height,
            instrument_height=instrument_height,
            azimuth_offset_rad=0.0,
            z_is_instrument_center=False,
        )


@dataclass(frozen=True)
class ResectionSighting:
    datum: Datum
    slope_distance: float
    horizontal_angle_rad: float
    vertical_angle_rad: float
    prism_height: float
    prism_offset: float = 0.0


@dataclass(frozen=True)
class SightingResidual:
    datum_name: str
    dx: float
    dy: float
    dz: float
    error_2d: float
    error_3d: float
    expected_distance: float = 0.0
    recorded_distance: float = 0.0
    distance_difference: float = 0.0


@dataclass(frozen=True)
class ResectionResult:
    station: StationState
    residuals: List[SightingResidual] = field(default_factory=list)
    rms_2d: float = 0.0
    rms_z: float = 0.0
    rms_3d: float = 0.0


def normalize_angle_rad(angle_rad: float) -> float:
    return angle_rad % (2.0 * math.pi)


def calc_angle_dddmmss(
    x_occ: float,
    y_occ: float,
    x_ref: float,
    y_ref: float,
    truncate_seconds: bool = False,
) -> Tuple[int, int, int, str]:
    """
    Exact replica of FUN_004b5fb0 (CalcAngle in frmStationSetup) & angle_between_xy_pairs (surf3-edm):
    Computes angle from (x_occ, y_occ) to (x_ref, y_ref) and returns
    (degrees, minutes, seconds, 'DDD.MMSS').
    """
    dx = x_ref - x_occ
    dy = y_ref - y_occ
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return (0, 0, 0, "0.0000")

    az_deg = math.degrees(math.atan2(dx, dy)) % 360.0
    deg = int(az_deg)
    rem_sec = (az_deg - deg) * 3600.0
    minutes = int(rem_sec // 60.0)
    if truncate_seconds:
        seconds = int((rem_sec - minutes * 60.0) + 1e-9)
    else:
        seconds = int(round(rem_sec - minutes * 60.0))
    if seconds >= 60:
        seconds -= 60
        minutes += 1
    if minutes >= 60:
        minutes -= 60
        deg = (deg + 1) % 360
    return (deg, minutes, seconds, f"{deg}.{minutes:02d}{seconds:02d}")


def flip_180_dddmmss(dddmmss_str: str) -> str:
    """
    Replicates 'Flip 180 Degrees' (calccomp on frmStationSetup):
    Adds or subtracts 180 degrees from a DDD.MMSS string while preserving MMSS.
    """
    clean = dddmmss_str.strip()
    if not clean:
        return "180.0000"
    if "." in clean:
        deg_part, frac_part = clean.split(".", 1)
    else:
        deg_part, frac_part = clean, "0000"
    deg = (int(deg_part) + 180) % 360
    frac_padded = (frac_part + "0000")[:4]
    return f"{deg}.{frac_padded}"


def reduce_polar_to_cartesian(
    station: StationState,
    slope_distance: float,
    horizontal_angle_rad: float,
    vertical_angle_rad: float,
    prism_height: float,
    prism_offset: float = 0.0,
) -> Tuple[Point3D, float, float]:
    corrected_sd = slope_distance + prism_offset
    if corrected_sd < 0.0:
        raise ValueError(f"Corrected slope distance cannot be negative ({corrected_sd:.4f} m).")

    hd = corrected_sd * math.sin(vertical_angle_rad)
    vd = corrected_sd * math.cos(vertical_angle_rad)
    effective_azimuth = normalize_angle_rad(horizontal_angle_rad + station.azimuth_offset_rad)

    x = station.x + hd * math.sin(effective_azimuth)
    y = station.y + hd * math.cos(effective_azimuth)
    z = station.instrument_center_z + vd - prism_height

    return Point3D(x=x, y=y, z=z), hd, vd


def initialize_direct(
    occupied_datum: Datum,
    instrument_height: float,
    azimuth_offset_rad: float = 0.0,
) -> StationState:
    return StationState(
        setup_method="Direct",
        station_name=occupied_datum.name,
        x=occupied_datum.x,
        y=occupied_datum.y,
        z=occupied_datum.z,
        instrument_height=instrument_height,
        azimuth_offset_rad=normalize_angle_rad(azimuth_offset_rad),
        rms_error_2d=0.0,
        rms_error_z=0.0,
    )


def initialize_one_point(
    target_datum: Datum,
    instrument_height: float,
    prism_height: float,
    slope_distance: float,
    horizontal_angle_rad: float,
    vertical_angle_rad: float,
    prism_offset: float = 0.0,
    azimuth_offset_rad: float = 0.0,
) -> StationState:
    corrected_sd = slope_distance + prism_offset
    hd = corrected_sd * math.sin(vertical_angle_rad)
    vd = corrected_sd * math.cos(vertical_angle_rad)

    eff_az = normalize_angle_rad(horizontal_angle_rad + azimuth_offset_rad)
    stn_x = target_datum.x - hd * math.sin(eff_az)
    stn_y = target_datum.y - hd * math.cos(eff_az)
    stn_z = target_datum.z - vd - instrument_height + prism_height

    return StationState(
        setup_method="1-Point",
        station_name=f"1PT->{target_datum.name}",
        x=stn_x,
        y=stn_y,
        z=stn_z,
        instrument_height=instrument_height,
        azimuth_offset_rad=normalize_angle_rad(azimuth_offset_rad),
        rms_error_2d=0.0,
        rms_error_z=0.0,
    )


def initialize_two_point_backsight(
    occupied_datum: Datum,
    backsight_datum: Datum,
    instrument_height: float,
    prism_height: float,
    slope_distance: float,
    horizontal_angle_rad: float,
    vertical_angle_rad: float,
    prism_offset: float = 0.0,
    solve_station_z_from_backsight: bool = False,
) -> Tuple[StationState, SightingResidual]:
    dx_true = backsight_datum.x - occupied_datum.x
    dy_true = backsight_datum.y - occupied_datum.y
    true_dist_2d = math.hypot(dx_true, dy_true)
    if true_dist_2d < 1e-6:
        raise ValueError(
            "You have to select two different datums: the one you are currently setup over and a reference datum."
        )

    grid_azimuth = math.atan2(dx_true, dy_true) % (2.0 * math.pi)
    azimuth_offset = normalize_angle_rad(grid_azimuth - horizontal_angle_rad)

    stn_z = occupied_datum.z
    if solve_station_z_from_backsight:
        corr_sd = slope_distance + prism_offset
        vd = corr_sd * math.cos(vertical_angle_rad)
        stn_z = backsight_datum.z - (vd - prism_height) - instrument_height

    station = StationState(
        setup_method="2-Point Backsight",
        station_name=f"{occupied_datum.name}->{backsight_datum.name}",
        x=occupied_datum.x,
        y=occupied_datum.y,
        z=stn_z,
        instrument_height=instrument_height,
        azimuth_offset_rad=azimuth_offset,
    )

    residual = verify_station(
        station=station,
        known_datum=backsight_datum,
        slope_distance=slope_distance,
        horizontal_angle_rad=horizontal_angle_rad,
        vertical_angle_rad=vertical_angle_rad,
        prism_height=prism_height,
        prism_offset=prism_offset,
    )
    station.rms_error_2d = residual.error_2d
    station.rms_error_z = abs(residual.dz)
    return station, residual


def solve_two_shot_setup(
    datum1: Datum,
    obs1_local_xyz: Point3D,
    datum2: Datum,
    obs2_local_xyz: Point3D,
) -> Tuple[Point3D, float, float, float, float]:
    """
    Exact replica of solve_two_shot_setup in surf3-edm (totalstation.py:451) & EDMWin Mode 3:
    Assumes datum1 was recorded with Horizontal Angle set to 0.0000.
    Returns (new_station_xyz, new_foresight_deg, measured_distance, actual_distance, error_distance).
    """
    measured_distance = math.sqrt(
        (obs1_local_xyz.x - obs2_local_xyz.x) ** 2
        + (obs1_local_xyz.y - obs2_local_xyz.y) ** 2
        + (obs1_local_xyz.z - obs2_local_xyz.z) ** 2
    )
    actual_distance = math.sqrt(
        (datum1.x - datum2.x) ** 2
        + (datum1.y - datum2.y) ** 2
        + (datum1.z - datum2.z) ** 2
    )
    error_distance = abs(measured_distance - actual_distance)

    dy_local = obs2_local_xyz.y - obs1_local_xyz.y
    dx_local = obs2_local_xyz.x
    measured_angle = math.degrees(math.atan2(dx_local, dy_local)) % 360.0

    defined_angle = math.degrees(math.atan2(datum2.x - datum1.x, datum2.y - datum1.y)) % 360.0
    angle_diff_rad = math.radians(defined_angle - measured_angle)

    stn_x = round(-1.0 * obs1_local_xyz.y * math.sin(angle_diff_rad) + datum1.x, 3)
    stn_y = round(-1.0 * obs1_local_xyz.y * math.cos(angle_diff_rad) + datum1.y, 3)
    z1 = datum1.z - obs1_local_xyz.z
    z2 = datum2.z - obs2_local_xyz.z
    stn_z = round((z1 + z2) / 2.0, 3)
    new_station = Point3D(stn_x, stn_y, stn_z)

    new_foresight_deg = math.degrees(math.atan2(datum2.x - new_station.x, datum2.y - new_station.y)) % 360.0
    return new_station, new_foresight_deg, measured_distance, actual_distance, error_distance


def _svd_2x2(a: float, b: float, c: float, d: float) -> Tuple[List[List[float]], List[List[float]]]:
    """Closed-form 2x2 SVD matching surf3-edm totalstation.py:482."""
    theta_v = 0.5 * math.atan2(2.0 * (a * c + b * d), (a ** 2 + b ** 2 - c ** 2 - d ** 2))
    theta_u = 0.5 * math.atan2(2.0 * (a * b + c * d), (a ** 2 - b ** 2 + c ** 2 - d ** 2))
    cu, su = math.cos(theta_u), math.sin(theta_u)
    cv, sv = math.cos(theta_v), math.sin(theta_v)
    U = [[cu, -su], [su, cu]]
    Vt = [[cv, sv], [-sv, cv]]
    return U, Vt


def compute_free_station_weighted_svd(
    sightings: List[ResectionSighting],
    instrument_height: float = 0.0,
    station_name: str = "Unknown",
    sigma_dist: float = 0.003,
    sigma_angle_sec: float = 5.0,
) -> Tuple[ResectionResult, Dict[str, Any]]:
    """
    Exact replica of surf3-edm compute_free_station (totalstation.py:497):
    Uses variance-weighted 2x2 closed-form SVD with distance-dependent angular variance.
    """
    if len(sightings) < 2:
        raise ValueError("At least 2 active datums are required.")

    sigma_angle_rad = math.radians(sigma_angle_sec / 3600.0)
    P_local: List[Tuple[float, float, float]] = []
    P_world: List[Tuple[float, float, float]] = []
    weights: List[float] = []
    ids: List[str] = []

    for s in sightings:
        corr_sd = s.slope_distance + s.prism_offset
        hd = corr_sd * math.sin(s.vertical_angle_rad)
        vd = corr_sd * math.cos(s.vertical_angle_rad)
        x_loc = hd * math.sin(s.horizontal_angle_rad)
        y_loc = hd * math.cos(s.horizontal_angle_rad)
        z_loc = vd - s.prism_height

        variance_i = (sigma_dist ** 2) + ((hd * sigma_angle_rad) ** 2)
        w_i = 1.0 / max(variance_i, 1e-12)
        P_local.append((x_loc, y_loc, z_loc))
        P_world.append((s.datum.x, s.datum.y, s.datum.z))
        weights.append(w_i)
        ids.append(s.datum.name)

    n = len(ids)
    sum_w = sum(weights)
    w_norm = [w / sum_w for w in weights]

    Z0_center = sum(w_norm[i] * (P_world[i][2] - P_local[i][2]) for i in range(n))
    stn_ground_z = Z0_center - instrument_height

    loc_cx = sum(w_norm[i] * P_local[i][0] for i in range(n))
    loc_cy = sum(w_norm[i] * P_local[i][1] for i in range(n))
    wrd_cx = sum(w_norm[i] * P_world[i][0] for i in range(n))
    wrd_cy = sum(w_norm[i] * P_world[i][1] for i in range(n))

    a = b = c = d = 0.0
    for i in range(n):
        dx_loc = P_local[i][0] - loc_cx
        dy_loc = P_local[i][1] - loc_cy
        dx_wrd = P_world[i][0] - wrd_cx
        dy_wrd = P_world[i][1] - wrd_cy
        w = weights[i]
        a += w * dx_loc * dx_wrd
        b += w * dx_loc * dy_wrd
        c += w * dy_loc * dx_wrd
        d += w * dy_loc * dy_wrd

    U, Vt = _svd_2x2(a, b, c, d)
    r00 = Vt[0][0] * U[0][0] + Vt[1][0] * U[0][1]
    r01 = Vt[0][0] * U[1][0] + Vt[1][0] * U[1][1]
    r10 = Vt[0][1] * U[0][0] + Vt[1][1] * U[0][1]
    r11 = Vt[0][1] * U[1][0] + Vt[1][1] * U[1][1]

    if (r00 * r11 - r01 * r10) < 0:
        r01 = -r01
        r11 = -r11

    t0_rad = math.atan2(r01, r00) % (2.0 * math.pi)
    X0 = wrd_cx - (r00 * loc_cx + r01 * loc_cy)
    Y0 = wrd_cy - (r10 * loc_cx + r11 * loc_cy)

    station = StationState(
        setup_method=f"Resection ({n}-Pt SVD)",
        station_name=station_name,
        x=X0,
        y=Y0,
        z=stn_ground_z,
        instrument_height=instrument_height,
        azimuth_offset_rad=t0_rad,
    )

    residuals: List[SightingResidual] = []
    datum_errors: Dict[str, Dict[str, float]] = {}
    total_sq_residuals = 0.0

    for i, s in enumerate(sightings):
        res = verify_station(
            station=station,
            known_datum=s.datum,
            slope_distance=s.slope_distance,
            horizontal_angle_rad=s.horizontal_angle_rad,
            vertical_angle_rad=s.vertical_angle_rad,
            prism_height=s.prism_height,
            prism_offset=s.prism_offset,
        )
        residuals.append(res)
        sigma_i = math.sqrt(1.0 / weights[i])
        std_res = res.error_3d / sigma_i
        total_sq_residuals += res.error_3d ** 2
        datum_errors[s.datum.name] = {
            "east_residual_m": round(-res.dx, 4),
            "north_residual_m": round(-res.dy, 4),
            "elev_residual_m": round(-res.dz, 4),
            "total_error_m": round(res.error_3d, 4),
            "std_residual_score": round(std_res, 2),
            "weight_percent": round(w_norm[i] * 100.0, 1),
        }

    dof = max(1, (2 * n) - 3)
    sigma_0 = math.sqrt(total_sq_residuals / dof)
    rms_2d = math.sqrt(sum(r.error_2d ** 2 for r in residuals) / n)
    rms_z = math.sqrt(sum(r.dz ** 2 for r in residuals) / n)
    rms_3d = math.sqrt(sum(r.error_3d ** 2 for r in residuals) / n)
    station.rms_error_2d = rms_2d
    station.rms_error_z = rms_z

    svd_meta = {
        "station_coords": Point3D(round(X0, 3), round(Y0, 3), round(Z0_center, 3)),
        "orientation_deg": round(math.degrees(t0_rad), 4),
        "orientation_rad": round(t0_rad, 6),
        "global_std_error_m": round(sigma_0, 4),
        "datum_errors": datum_errors,
    }
    return ResectionResult(station=station, residuals=residuals, rms_2d=rms_2d, rms_z=rms_z, rms_3d=rms_3d), svd_meta


def initialize_resection(
    sightings: List[ResectionSighting],
    instrument_height: float,
    station_name: str = "Unknown",
    use_weighted_svd: bool = False,
) -> ResectionResult:
    if len(sightings) < 2:
        raise ValueError("Resection requires at least 2 sightings to known Datums.")

    if use_weighted_svd:
        res_svd, _ = compute_free_station_weighted_svd(
            sightings=sightings,
            instrument_height=instrument_height,
            station_name=station_name,
        )
        return res_svd

    n = len(sightings)
    local_pts: List[Tuple[float, float, float, float]] = []
    for s in sightings:
        corr_sd = s.slope_distance + s.prism_offset
        hd = corr_sd * math.sin(s.vertical_angle_rad)
        vd = corr_sd * math.cos(s.vertical_angle_rad)
        x_loc = hd * math.sin(s.horizontal_angle_rad)
        y_loc = hd * math.cos(s.horizontal_angle_rad)
        local_pts.append((x_loc, y_loc, hd, vd))

    mean_x_loc = sum(p[0] for p in local_pts) / n
    mean_y_loc = sum(p[1] for p in local_pts) / n
    mean_X = sum(s.datum.x for s in sightings) / n
    mean_Y = sum(s.datum.y for s in sightings) / n

    c_term = 0.0
    s_term = 0.0
    for (x_loc, y_loc, _, _), s in zip(local_pts, sightings):
        dx_loc = x_loc - mean_x_loc
        dy_loc = y_loc - mean_y_loc
        dX = s.datum.x - mean_X
        dY = s.datum.y - mean_Y
        c_term += dx_loc * dX + dy_loc * dY
        s_term += dy_loc * dX - dx_loc * dY

    if abs(c_term) < 1e-12 and abs(s_term) < 1e-12:
        raise ValueError("Degenerate resection geometry: Datums are coincident.")

    omega = normalize_angle_rad(math.atan2(s_term, c_term))
    stn_x = mean_X - (mean_x_loc * math.cos(omega) + mean_y_loc * math.sin(omega))
    stn_y = mean_Y - (-mean_x_loc * math.sin(omega) + mean_y_loc * math.cos(omega))

    for _ in range(15):
        grad_x = 0.0
        grad_y = 0.0
        grad_omega = 0.0
        denom_omega = 0.0
        for (_, _, hd, _), s in zip(local_pts, sightings):
            az = s.horizontal_angle_rad + omega
            pred_x = stn_x + hd * math.sin(az)
            pred_y = stn_y + hd * math.cos(az)
            rx = s.datum.x - pred_x
            ry = s.datum.y - pred_y
            grad_x += rx
            grad_y += ry
            dx_omega = hd * math.cos(az)
            dy_omega = -hd * math.sin(az)
            grad_omega += dx_omega * rx + dy_omega * ry
            denom_omega += dx_omega * dx_omega + dy_omega * dy_omega

        d_stn_x = grad_x / n
        d_stn_y = grad_y / n
        d_omega = grad_omega / denom_omega if denom_omega > 1e-12 else 0.0

        stn_x += d_stn_x
        stn_y += d_stn_y
        omega = normalize_angle_rad(omega + d_omega)
        if math.hypot(d_stn_x, d_stn_y) < 1e-9 and abs(d_omega) < 1e-9:
            break

    z_estimates = [
        s.datum.z - local_pts[idx][3] - instrument_height + s.prism_height
        for idx, s in enumerate(sightings)
    ]
    stn_z = sum(z_estimates) / n

    station = StationState(
        setup_method=f"Resection ({n}-Pt)",
        station_name=station_name,
        x=stn_x,
        y=stn_y,
        z=stn_z,
        instrument_height=instrument_height,
        azimuth_offset_rad=omega,
    )

    residuals: List[SightingResidual] = []
    for s in sightings:
        res = verify_station(
            station=station,
            known_datum=s.datum,
            slope_distance=s.slope_distance,
            horizontal_angle_rad=s.horizontal_angle_rad,
            vertical_angle_rad=s.vertical_angle_rad,
            prism_height=s.prism_height,
            prism_offset=s.prism_offset,
        )
        residuals.append(res)

    rms_2d = math.sqrt(sum(r.error_2d ** 2 for r in residuals) / n)
    rms_z = math.sqrt(sum(r.dz ** 2 for r in residuals) / n)
    rms_3d = math.sqrt(sum(r.error_3d ** 2 for r in residuals) / n)

    station.rms_error_2d = rms_2d
    station.rms_error_z = rms_z

    return ResectionResult(
        station=station,
        residuals=residuals,
        rms_2d=rms_2d,
        rms_z=rms_z,
        rms_3d=rms_3d,
    )


def verify_station(
    station: StationState,
    known_datum: Datum,
    slope_distance: float,
    horizontal_angle_rad: float,
    vertical_angle_rad: float,
    prism_height: float,
    prism_offset: float = 0.0,
) -> SightingResidual:
    measured_pt, hd, _ = reduce_polar_to_cartesian(
        station=station,
        slope_distance=slope_distance,
        horizontal_angle_rad=horizontal_angle_rad,
        vertical_angle_rad=vertical_angle_rad,
        prism_height=prism_height,
        prism_offset=prism_offset,
    )
    dx = measured_pt.x - known_datum.x
    dy = measured_pt.y - known_datum.y
    dz = measured_pt.z - known_datum.z
    err_2d = math.hypot(dx, dy)
    err_3d = math.sqrt(dx * dx + dy * dy + dz * dz)

    expected_dist_2d = math.hypot(known_datum.x - station.x, known_datum.y - station.y)
    dist_diff = hd - expected_dist_2d

    return SightingResidual(
        datum_name=known_datum.name,
        dx=dx,
        dy=dy,
        dz=dz,
        error_2d=err_2d,
        error_3d=err_3d,
        expected_distance=expected_dist_2d,
        recorded_distance=hd,
        distance_difference=dist_diff,
    )


def check_point_in_unit(point: Point3D, unit: ExcavationUnit) -> Tuple[bool, str]:
    shape = unit.shape.strip().upper()
    if shape == "NOLIMITS" or unit.min_x == -99999.0:
        return True, "OK (No Limits)"
    if shape == "CIRCLE":
        dist_xy = math.hypot(point.x - unit.center_x, point.y - unit.center_y)
        if dist_xy > unit.radius:
            return (
                False,
                f"Point ({point.x:.3f}, {point.y:.3f}) is {dist_xy:.3f}m from unit center "
                f"({unit.center_x:.3f}, {unit.center_y:.3f}), exceeding radius {unit.radius:.3f}m.",
            )
    else:
        low_x, high_x = min(unit.min_x, unit.max_x), max(unit.min_x, unit.max_x)
        low_y, high_y = min(unit.min_y, unit.max_y), max(unit.min_y, unit.max_y)
        if not (low_x <= point.x <= high_x and low_y <= point.y <= high_y):
            return (
                False,
                f"Point ({point.x:.3f}, {point.y:.3f}) lies outside Unit '{unit.name}' "
                f"X:[{low_x:.3f}, {high_x:.3f}], Y:[{low_y:.3f}, {high_y:.3f}].",
            )

    if unit.min_z is not None and point.z < unit.min_z:
        return (
            False,
            f"Point Z ({point.z:.3f}) is below Unit '{unit.name}' minimum Z ({unit.min_z:.3f}).",
        )
    if unit.max_z is not None and point.z > unit.max_z:
        return (
            False,
            f"Point Z ({point.z:.3f}) is above Unit '{unit.name}' maximum Z ({unit.max_z:.3f}).",
        )

    return True, "OK"


def auto_find_unit(
    point: Point3D,
    units: List[ExcavationUnit],
) -> Optional[ExcavationUnit]:
    """
    Replicates FindUnit in edmwin.exe (0x0042d7d4 / 0x0042d648):
    1. Searches bounded Rectangular units where minx <= X <= maxx and miny <= Y <= maxy.
    2. Searches bounded Circular units where abs(centerx - X) <= radius and abs(centery - Y) <= radius
       and radial distance <= radius.
    Returns the matching ExcavationUnit or None.
    """
    for u in units:
        if u.shape.upper() == "NOLIMITS" or u.min_x == -99999.0:
            continue
        ok, _ = check_point_in_unit(point, u)
        if ok:
            return u
    return None


def apply_directional_offset(
    point: Point3D,
    direction: str,
    amount_meters: float,
) -> Point3D:
    """
    Replicates frmOffSet in edmwin.exe (0x0042cb2c - 0x0042cbbc & 0x004333fc):
    Supports 'Offset East' (+X), 'Offset West' (-X), 'Offset North' (+Y),
    'Offset South' (-Y), 'Offset Up' (+Z), and 'Offset Down' (-Z).
    Enforces the 1-kilometer sanity guard from 0x004333fc.
    """
    if abs(amount_meters) > 1000.0:
        raise ValueError(
            "Offsetting a point by more than a kilometer does not seem right -- "
            "if that's the case, then do it in increments"
        )
    d = direction.strip().upper()
    if "EAST" in d:
        return Point3D(point.x + amount_meters, point.y, point.z)
    if "WEST" in d:
        return Point3D(point.x - amount_meters, point.y, point.z)
    if "NORTH" in d:
        return Point3D(point.x, point.y + amount_meters, point.z)
    if "SOUTH" in d:
        return Point3D(point.x, point.y - amount_meters, point.z)
    if "UP" in d:
        return Point3D(point.x, point.y, point.z + amount_meters)
    if "DOWN" in d:
        return Point3D(point.x, point.y, point.z - amount_meters)
    raise ValueError(f"Unknown offset direction: {direction!r}")


def format_speech_unit_id(unit: str, point_id: str) -> str:
    """
    Exact replica of FUN_004c2b70 (Speak Unit-ID on new shots):
    Spaces out multi-character unit codes so TTS reads individual letters/digits,
    followed by ' dash ' and the ID.
    """
    u_clean = unit.strip()
    id_clean = str(point_id).strip()
    try:
        is_zero_val = float(u_clean) == 0.0
    except ValueError:
        is_zero_val = True

    if is_zero_val and len(u_clean) > 1:
        spaced_unit = " ".join(list(u_clean))
    else:
        spaced_unit = u_clean
    return f"{spaced_unit} dash {id_clean}"


def _vec_sub(a: Point3D, b: Point3D) -> Point3D:
    return Point3D(a.x - b.x, a.y - b.y, a.z - b.z)


def _vec_dot(a: Point3D, b: Point3D) -> float:
    return a.x * b.x + a.y * b.y + a.z * b.z


def _vec_cross(a: Point3D, b: Point3D) -> Point3D:
    return Point3D(
        a.y * b.z - a.z * b.y,
        a.z * b.x - a.x * b.z,
        a.x * b.y - a.y * b.x,
    )


def _vec_norm(a: Point3D) -> Point3D:
    mag = math.sqrt(_vec_dot(a, a))
    if mag < 1e-12:
        return a
    return Point3D(a.x / mag, a.y / mag, a.z / mag)


def _rotate_point_rodrigues(target_vec: Point3D, source_vec: Point3D, p: Point3D) -> Point3D:
    """
    Exact replica of rotate_point_2d in surf3-edm (totalstation.py:706):
    Rotates 3D point `p` using the skew-symmetric cross-product matrix aligning `source_vec` with `target_vec`.
     Note: follows surf3-edm's exact row-column multiplication convention (p * R^T) where
    v = target_vec x source_vec.
    """
    v = _vec_cross(target_vec, source_vec)
    c = _vec_dot(target_vec, source_vec)
    if abs(1.0 + c) < 1e-12:
        return Point3D(-p.x, -p.y, -p.z)

    vx = [
        [0.0, -v.z, v.y],
        [v.z, 0.0, -v.x],
        [-v.y, v.x, 0.0],
    ]
    vx2 = [
        [sum(vx[i][k] * vx[k][j] for k in range(3)) / (1.0 + c) for j in range(3)]
        for i in range(3)
    ]
    r = [
        [(1.0 if i == j else 0.0) + vx[i][j] + vx2[i][j] for j in range(3)]
        for i in range(3)
    ]
    return Point3D(
        p.x * r[0][0] + p.y * r[1][0] + p.z * r[2][0],
        p.x * r[0][1] + p.y * r[1][1] + p.z * r[2][1],
        p.x * r[0][2] + p.y * r[1][2] + p.z * r[2][2],
    )


def solve_3d_block_georeference(
    local_points: List[Point3D],
    grid_points: List[Point3D],
    query_local: Point3D,
) -> Point3D:
    """
    Exact replica of surf3-edm rotate_point (totalstation.py:766):
    Performs full 3D triangle-leg + surface-normal alignment for 3 paired control points,
    or 7-parameter 3D rigid Helmert transformation if >3 control points are provided.
    """
    if len(local_points) < 3 or len(local_points) != len(grid_points):
        raise ValueError("3D block georeferencing requires at least 3 paired control points.")

    if len(local_points) > 3:
        rotator, _ = compute_helmert_transformation(local_points, grid_points, fix_scale_to_one=True)
        return helmert_3d_transform(query_local, rotator, round_digits=None)

    p_rel = _vec_sub(query_local, local_points[0])
    loc1 = _vec_sub(local_points[1], local_points[0])
    loc2 = _vec_sub(local_points[2], local_points[0])

    local_vec = _vec_norm(loc1)
    global_vec = _vec_norm(_vec_sub(grid_points[1], grid_points[0]))
    p_out = _rotate_point_rodrigues(global_vec, local_vec, p_rel)

    rot_loc1 = _rotate_point_rodrigues(global_vec, local_vec, loc1)
    rot_loc2 = _rotate_point_rodrigues(global_vec, local_vec, loc2)

    local_normal = _vec_norm(_vec_cross(_vec_norm(rot_loc1), _vec_norm(rot_loc2)))
    nv1 = _vec_norm(_vec_sub(grid_points[1], grid_points[0]))
    nv2 = _vec_norm(_vec_sub(grid_points[2], grid_points[0]))
    global_normal = _vec_norm(_vec_cross(nv1, nv2))

    # If the 3 points are collinear (zero normal), fall back to first-stage 3D leg alignment
    if math.sqrt(_vec_dot(local_normal, local_normal)) < 1e-12 or math.sqrt(_vec_dot(global_normal, global_normal)) < 1e-12:
        p_out2 = p_out
    else:
        p_out2 = _rotate_point_rodrigues(global_normal, local_normal, p_out)

    return Point3D(
        x=p_out2.x + grid_points[0].x,
        y=p_out2.y + grid_points[0].y,
        z=p_out2.z + grid_points[0].z,
    )


def _helmert_rotation_matrix(omega: float, phi: float, kappa: float) -> List[List[float]]:
    co, so = math.cos(omega), math.sin(omega)
    cp, sp = math.cos(phi), math.sin(phi)
    ck, sk = math.cos(kappa), math.sin(kappa)
    R_x = [[1.0, 0.0, 0.0], [0.0, co, -so], [0.0, so, co]]
    R_y = [[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]]
    R_z = [[ck, -sk, 0.0], [sk, ck, 0.0], [0.0, 0.0, 1.0]]
    R_zy = [[sum(R_z[i][k] * R_y[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return [[sum(R_zy[i][k] * R_x[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def helmert_residuals(
    params: List[float],
    source_pts: List[Point3D],
    target_pts: List[Point3D],
) -> List[float]:
    tx, ty, tz, omega, phi, kappa, s = params
    R = _helmert_rotation_matrix(omega, phi, kappa)
    T = [tx, ty, tz]
    res: List[float] = []
    for p_src, p_tgt in zip(source_pts, target_pts):
        p = [p_src.x, p_src.y, p_src.z]
        t_pt = [T[i] + s * sum(R[i][j] * p[j] for j in range(3)) for i in range(3)]
        res.extend([t_pt[0] - p_tgt.x, t_pt[1] - p_tgt.y, t_pt[2] - p_tgt.z])
    return res


def compute_helmert_transformation(
    source_points: List[Point3D],
    target_points: List[Point3D],
    fix_scale_to_one: bool = True,
) -> Tuple[Dict[str, float], Dict[int, List[float]]]:
    """
    Exact replica of surf3-edm compute_helmert_transformation / calculate_helmert_parameters
    (totalstation.py:604-725): Levenberg-Marquardt 6/7-parameter 3D Helmert solver.
    """
    if len(source_points) < 3 or len(source_points) != len(target_points):
        raise ValueError("Helmert 3D transformation requires at least 3 paired points.")

    params = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    active = list(range(6)) if fix_scale_to_one else list(range(7))
    residual = helmert_residuals(params, source_points, target_points)
    cost = sum(v * v for v in residual)
    damping = 1e-3

    for _ in range(200):
        jacobian = [[0.0] * len(active) for _ in residual]
        for col, idx in enumerate(active):
            step = 1e-6 * max(1.0, abs(params[idx]))
            perturbed = params[:]
            perturbed[idx] += step
            shifted = helmert_residuals(perturbed, source_points, target_points)
            for row in range(len(residual)):
                jacobian[row][col] = (shifted[row] - residual[row]) / step

        size = len(active)
        normal = [
            [sum(jacobian[k][i] * jacobian[k][j] for k in range(len(residual))) for j in range(size)]
            for i in range(size)
        ]
        gradient = [sum(jacobian[k][i] * residual[k] for k in range(len(residual))) for i in range(size)]
        for i in range(size):
            normal[i][i] += damping * max(normal[i][i], 1.0)

        matrix = [normal[i][:] + [-gradient[i]] for i in range(size)]
        singular = False
        for col in range(size):
            pivot = max(range(col, size), key=lambda r: abs(matrix[r][col]))
            if abs(matrix[pivot][col]) < 1e-20:
                singular = True
                break
            matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
            div = matrix[col][col]
            matrix[col] = [val / div for val in matrix[col]]
            for row in range(size):
                if row != col:
                    fac = matrix[row][col]
                    matrix[row] = [matrix[row][j] - fac * matrix[col][j] for j in range(size + 1)]
        if singular:
            break

        delta = [matrix[i][-1] for i in range(size)]
        cand = params[:]
        for i, idx in enumerate(active):
            cand[idx] += delta[i]
        cand_res = helmert_residuals(cand, source_points, target_points)
        cand_cost = sum(v * v for v in cand_res)
        if cand_cost < cost:
            params, residual, cost = cand, cand_res, cand_cost
            damping = max(damping / 3.0, 1e-12)
            if max(abs(v) for v in delta) < 1e-10:
                break
        else:
            damping = min(damping * 10.0, 1e12)

    tx, ty, tz, omega, phi, kappa, s = params
    rotator = {"tx": tx, "ty": ty, "tz": tz, "omega": omega, "phi": phi, "kappa": kappa, "scale": s}

    errors: Dict[int, List[float]] = {}
    for i, (src, tgt) in enumerate(zip(source_points, target_points)):
        t_pt = helmert_3d_transform(src, rotator, round_digits=None)
        dx, dy, dz = t_pt.x - tgt.x, t_pt.y - tgt.y, t_pt.z - tgt.z
        err_3d = math.sqrt(dx * dx + dy * dy + dz * dz)
        errors[i] = [dx, dy, dz, err_3d]

    return rotator, errors


def helmert_3d_transform(
    pt: Point3D,
    rotator: Dict[str, float],
    round_digits: Optional[int] = 3,
) -> Point3D:
    tx = rotator["tx"]
    ty = rotator["ty"]
    tz = rotator["tz"]
    omega = rotator["omega"]
    phi = rotator["phi"]
    kappa = rotator["kappa"]
    s = rotator.get("scale", rotator.get("s", 1.0))
    R = _helmert_rotation_matrix(omega, phi, kappa)
    p = [pt.x, pt.y, pt.z]
    rot = [sum(R[i][j] * p[j] for j in range(3)) for i in range(3)]
    x_out = tx + s * rot[0]
    y_out = ty + s * rot[1]
    z_out = tz + s * rot[2]
    if round_digits is not None:
        return Point3D(round(x_out, round_digits), round(y_out, round_digits), round(z_out, round_digits))
    return Point3D(x_out, y_out, z_out)


def evaluate_coordinate_math(expr: str, current_val: Optional[float] = None) -> float:
    """
    Safe arithmetic evaluator matching surf3-edm do_coordinate_math (e5_widgets.py:216):
    - Supports full arithmetic expressions ('100.25 + 0.50', '10.5 - 1.2', '2 * 3.5')
    - Also supports relative '+0.25' / '-1.50' offsets when current_val is provided.
    """
    clean = expr.strip()
    if not clean:
        raise ValueError("Empty coordinate expression")
    if current_val is not None and (clean.startswith("+") or clean.startswith("-")) and all(
        c not in clean[1:] for c in "+-*/"
    ):
        return current_val + float(clean)

    allowed_ops = {
        ast.Add: _op.add,
        ast.Sub: _op.sub,
        ast.Mult: _op.mul,
        ast.Div: _op.truediv,
        ast.USub: _op.neg,
        ast.UAdd: _op.pos,
    }

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in allowed_ops:
            return float(allowed_ops[type(node.op)](_eval(node.left), _eval(node.right)))
        if isinstance(node, ast.UnaryOp) and type(node.op) in allowed_ops:
            return float(allowed_ops[type(node.op)](_eval(node.operand)))
        raise ValueError(f"Unsupported coordinate math expression: {expr!r}")

    return _eval(ast.parse(clean, mode="eval"))


def apply_coordinate_offset_expression(current_val: float, expr: str) -> float:
    return evaluate_coordinate_math(expr, current_val=current_val)
