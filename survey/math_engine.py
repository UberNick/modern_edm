"""
Surveying & Geodetic Math Engine for EDMWin Replacement.
Includes exact replicas of all decompiled edmwin.exe math routines:
- Polar-to-XYZ reduction (FUN_004c8840 / FUN_004cee60)
- Legacy StationZ (center-of-instrument) vs. Ground Datum elevation handling (frmStatus @ 0x0043c99f)
- Prism Height [-10, 10]m & Offset [-0.05, 0.05]m validation (frmPolesheet @ 0x0042f2d0 / 0x0042f398)
- CalcAngle 2-point azimuth in DDD.MMSS & (Deg, Min, Sec) (FUN_004b5fb0)
- Flip 180 Degrees (calccomp on frmStationSetup)
- Initialize Direct, 1-Point, 2-Point Backsight (with 3D distance error), and 2/3+ Point Resection (FUN_0050dac0 / FUN_00502330 / FUN_00549120)
- Verify Station (frmStationVerify)
- 6-Direction Post-Shot Coordinate Offset (frmOffSet: East, West, North, South, Up, Down)
- Auto-Find Unit (FindUnit SQL bounding-box & circle lookup + unbounded minx=-99999 units)
- SAPI Voice Readout Formatter (FUN_004c2b70: "<Unit> dash <ID>")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


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
) -> Tuple[int, int, int, str]:
    """
    Exact replica of FUN_004b5fb0 (CalcAngle in frmStationSetup):
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

    station = StationState(
        setup_method="2-Point Backsight",
        station_name=f"{occupied_datum.name}->{backsight_datum.name}",
        x=occupied_datum.x,
        y=occupied_datum.y,
        z=occupied_datum.z,
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


def initialize_resection(
    sightings: List[ResectionSighting],
    instrument_height: float,
    station_name: str = "Unknown",
) -> ResectionResult:
    if len(sightings) < 2:
        raise ValueError("Resection requires at least 2 sightings to known Datums.")

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


def solve_3d_block_georeference(
    local_points: List[Point3D],
    grid_points: List[Point3D],
    query_local: Point3D,
) -> Point3D:
    if len(local_points) < 3 or len(local_points) != len(grid_points):
        raise ValueError("3D block georeferencing requires at least 3 paired control points.")

    n = len(local_points)
    cx_l = sum(p.x for p in local_points) / n
    cy_l = sum(p.y for p in local_points) / n
    cz_l = sum(p.z for p in local_points) / n

    cx_g = sum(p.x for p in grid_points) / n
    cy_g = sum(p.y for p in grid_points) / n
    cz_g = sum(p.z for p in grid_points) / n

    c_term = 0.0
    s_term = 0.0
    for lp, gp in zip(local_points, grid_points):
        dlx, dly = lp.x - cx_l, lp.y - cy_l
        dgx, dgy = gp.x - cx_g, gp.y - cy_g
        c_term += dlx * dgx + dly * dgy
        s_term += dlx * dgy - dly * dgx

    theta = math.atan2(s_term, c_term)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)

    qx = query_local.x - cx_l
    qy = query_local.y - cy_l
    qz = query_local.z - cz_l

    X = cx_g + (qx * cos_t - qy * sin_t)
    Y = cy_g + (qx * sin_t + qy * cos_t)
    Z = cz_g + qz
    return Point3D(x=X, y=Y, z=Z)


def apply_coordinate_offset_expression(current_val: float, expr: str) -> float:
    clean = expr.strip()
    if clean.startswith("+") or clean.startswith("-"):
        return current_val + float(clean)
    return float(clean)
