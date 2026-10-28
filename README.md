# Modern EDM — Total Station Excavation & Archaeological Survey Recorder

**Modern EDM** is a cross-platform (Windows & macOS) Python/PySide6 replacement for the legacy Visual Basic 6 **EDM Windows (`edmwin.exe` v3.2 / v3.05.0010)** total-station surveying program originally developed by Harold L. Dibble and Shannon P. McPherron (`OldStoneAge.com`).

Built from a complete Ghidra decompilation and subsystem audit of `edmwin.exe`, Modern EDM preserves 100% wire-protocol, mathematical, and `.CFG` workflow parity with legacy archaeological excavations while replacing the 32-bit Microsoft Jet/DAO `.mdb` engine with crash-safe atomic SQLite (WAL) storage, live 2D Plan/Section plotting, and QGIS GeoJSON export.

---

## Key Features

### 1. Hardware Total Station & 3D Digitizer Drivers (`drivers/total_station.py`)
Supports all 9 instrument modes from `edmwin.exe`:
- **Leica GeoCOM (`WILD2`):** Standard ASCII RPC (`%R1Q,2008:1,1` trigger, `%R1Q,2108:10000,1` read, `%R1Q,2113:<rad>` horizontal angle preset, `%R1Q,5008:` date/time, `%R1Q,5004,6,21341:` instrument name).
- **Leica Motorized AutoTilt (`LEICA_AUTOTILT`):** Full 11-step GeoCOM target search, 360° prism lock, tilt-compensated measurement (`%R1Q,2187:10000,1`), and lock release.
- **Leica / Wild GSI-8 & GSI-16 (`WILD` / `LEICA`):** `SET/41/0`, `SET/149/2`, `GET/M/WI11/WI21/WI22/WI31/WI51`, and `PUT/21...4+` angle preset across gons, decimal degrees, DMS, mm, 0.1 mm, and millifeet.
- **Leica Builder 209 (`BUILDER`):** `$MSR` trigger and `/Dc ` horizontal angle preset.
- **Topcon (`TOPCON`):** GTS-3, GTS-210, GTS-220, GTS-301/302/303, GTS-320, and GM-55 (`\x06R\x03` / `\x15?\x03`, `!HAN` angle preset, 2-char hex & 3-digit decimal BCC checksum verification, and barrel-reversal checks).
- **Sokkia (`SOKKIA`) & Nikon (`NIKON`):** `DC1` (`\x11`) trigger, `DC2` (`\x12`) abort, Sokkia raw 8-digit angle preset, and Nikon `!HAN` preset.
- **Microscribe / Pentograph (`MICROSCRIBE` / `PENTOGRAPH`):** `GETPOS` serial polling and MUS clipboard `X,Y,Z` millimeter-to-meter block origin reduction.
- **Offline Simulator (`SIMULATE`):** Built-in deterministic synthetic total station for testing and training without hardware connected.

### 2. Geodetic & Surveying Math Engine (`survey/math_engine.py`)
- **Polar-to-Cartesian Reduction:** Zenith vertical angle, clockwise azimuth + station orientation offset, prism constant offset (`[-0.05, +0.05]` m) and pole height (`[-10.0, +10.0]` m, supporting negative heights for cave ceilings).
- **4 Station Initialization Modes:**
  1. **Direct / Set Angle Only** (with `Flip 180 Degrees` helper).
  2. **1-Point Setup** (unknown occupation shooting 1 known datum).
  3. **2-Point Backsight** (known occupation shooting a reference datum; automatically sets hardware horizontal circle and reports expected vs. recorded distance residual).
  4. **N-Point Free-Station Resection** (2+ known datums with least-squares refinement and 2D/3D RMS residual reporting).
- **Station Verify (`frmStationVerify`):** Shoots a known control nail and reports `(dX, dY, dZ)`, 2D/3D error, and horizontal distance residual.
- **6-Way Post-Shot Coordinate Offset (`frmOffSet`):** `Offset East`, `West`, `North`, `South`, `Up`, and `Down` with 1 km sanity guard and cross-unit boundary detection.

### 3. Legacy `.CFG` & Excavation Workflow Parity (`storage/cfg_parser.py`, `storage/database.py`)
- **Multi-Field SpeedButtons (`[BUTTON1]`–`[BUTTON6]`):** Supports `TITLE=`, `Shortcut=` (`Ctrl+Key`), and multi-field presets (including safe `UNIT` switching without skipping `ID` numbers).
- **Unit-Scoped Carryover Fields (`Unitfields=`):** Automatically saves and restores `ID`, `LEVEL`, and `EXCAVATOR` per excavation unit when switching units.
- **Multi-Shot Continuation (`+Shot`) & Series Resequencing:** Validates initial series records (`SUFFIX=0`), locks `UNIT` within an active series, and automatically resequences subsequent `SUFFIX` numbers if a point in a series is deleted.
- **Duplicate ID Resolution (`frmDupID`):** Interactive 3-way resolution (**Convert to alpha ID** `42a`, **New ID**, or **Continuation `+Shot`**).
- **Exporters & Label Printing:**
  - 4-Table Legacy EDMWin CSV Bundle (`_points.csv`, `_datums.csv`, `_prisms.csv`, `_units.csv`)
  - Single-file CSV Import/Export
  - QGIS GeoJSON Export (`Point` features + automatic `LineString` reconstruction for multi-shot sequences)
  - ASCII & Zebra ZPL II (Code-39 barcode) artifact bag label spooler (`storage/label_printer.py`)

---

## Quick Start (Development)

```bash
pip install -r requirements.txt
python main.py
```

### Run the Automated Parity Test Suite
```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

---

## Building Standalone Executables (Windows & macOS)

### Windows (`dist\ModernEDM.exe`)
```powershell
.\build_windows.bat
```

### macOS (`dist/ModernEDM.app` & `dist/ModernEDM-macOS.dmg`)
```bash
chmod +x build_macos.sh
./build_macos.sh
```

---

## License
MIT License — see [LICENSE](LICENSE) for details.
