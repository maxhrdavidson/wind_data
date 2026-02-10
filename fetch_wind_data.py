#!/usr/bin/env python3
"""
Fetch ASCE 7-22 wind speed data for solar project locations.

Reads latitude/longitude coordinates from the SEIA Solar Data Excel file,
queries the ASCE Hazard Tool API for wind data, and exports results to CSV.

Usage:
    python fetch_wind_data.py

    # Or override the token:
    export ASCE_API_TOKEN="your-api-key-here"
    python fetch_wind_data.py --token YOUR_API_KEY

    # Limit to first N rows (useful for testing):
    python fetch_wind_data.py --limit 10

    # Resume from a previous partial run:
    python fetch_wind_data.py --resume
"""

import argparse
import csv
import logging
import os
import sys
import time
from pathlib import Path

import requests
from openpyxl import load_workbook

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EXCEL_FILE = Path(__file__).parent / "SEIA Solar Data.xlsx"
OUTPUT_CSV = Path(__file__).parent / "wind_data_results.csv"
PROGRESS_CSV = Path(__file__).parent / ".wind_data_progress.csv"

API_BASE_URL = "https://api-hazard.asce.org/v1/wind"
STANDARDS_VERSION = "7-22"

# ASCE 7-22 wind speed maps by Risk Category / MRI:
#   Risk Category I  -> 300-year MRI  (Figure 26.5-1A)
#   Risk Category II -> 700-year MRI  (Figure 26.5-1B)
RISK_CATEGORY_300YR = "I"
RISK_CATEGORY_700YR = "II"

# Rate limiting: seconds between API requests
REQUEST_DELAY = 0.5

# Retry configuration for transient failures
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 2  # seconds; exponential: 2, 4, 8, 16

# Request timeout in seconds
REQUEST_TIMEOUT = 30

# CSV output columns
CSV_COLUMNS = [
    "lat",
    "lon",
    "plant_name",
    "state",
    "wind_speed_300yr",
    "wind_speed_700yr",
    "is_hurricane_zone",
    "is_special_wind_zone",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Excel reader
# ---------------------------------------------------------------------------


def read_coordinates(filepath: Path, limit: int | None = None) -> list[dict]:
    """Read lat/lon and metadata from the SEIA Solar Data Excel file.

    Returns a list of dicts with keys: lat, lon, plant_name, state.
    Rows with missing lat/lon are skipped.
    """
    logger.info("Loading workbook: %s", filepath)
    wb = load_workbook(filepath, read_only=True, data_only=True)
    ws = wb.active

    # Build header index from first row
    headers = []
    for cell in next(ws.iter_rows(min_row=1, max_row=1)):
        headers.append(str(cell.value).strip().lower() if cell.value else "")

    col_idx = {name: i for i, name in enumerate(headers)}

    required = ["latitude", "longitude"]
    for col in required:
        if col not in col_idx:
            raise ValueError(
                f"Column '{col}' not found in Excel file. "
                f"Available columns: {headers}"
            )

    lat_i = col_idx["latitude"]
    lon_i = col_idx["longitude"]
    name_i = col_idx.get("plant name")
    state_i = col_idx.get("state")

    records = []
    for row_num, row in enumerate(ws.iter_rows(min_row=2), start=2):
        lat = row[lat_i].value
        lon = row[lon_i].value

        if lat is None or lon is None:
            logger.debug("Skipping row %d: missing lat/lon", row_num)
            continue

        try:
            lat = float(lat)
            lon = float(lon)
        except (ValueError, TypeError):
            logger.warning("Skipping row %d: invalid lat/lon (%s, %s)", row_num, lat, lon)
            continue

        record = {
            "lat": lat,
            "lon": lon,
            "plant_name": row[name_i].value if name_i is not None else "",
            "state": row[state_i].value if state_i is not None else "",
        }
        records.append(record)

        if limit and len(records) >= limit:
            break

    wb.close()
    logger.info("Loaded %d coordinates from Excel file", len(records))
    return records


# ---------------------------------------------------------------------------
# ASCE Hazard Tool API client
# ---------------------------------------------------------------------------


def _api_request(
    lat: float,
    lon: float,
    risk_level: str,
    token: str,
    session: requests.Session,
) -> dict:
    """Make a single API request to the ASCE Hazard Tool.

    Returns the parsed JSON response.
    Raises on HTTP errors after retries are exhausted.
    """
    params = {
        "lat": lat,
        "lon": lon,
        "standardsVersion": STANDARDS_VERSION,
        "riskLevel": risk_level,
        "token": token,
    }

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(
                API_BASE_URL,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            # Don't retry client errors (except 429 rate limit)
            if status is not None and 400 <= status < 500 and status != 429:
                logger.error(
                    "Client error %s for (%s, %s) risk=%s: %s",
                    status, lat, lon, risk_level, exc,
                )
                raise
            last_error = exc
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc

        if attempt < MAX_RETRIES:
            wait = RETRY_BACKOFF_BASE ** attempt
            logger.warning(
                "Attempt %d/%d failed for (%s, %s). Retrying in %ds...",
                attempt, MAX_RETRIES, lat, lon, wait,
            )
            time.sleep(wait)

    raise RuntimeError(
        f"All {MAX_RETRIES} attempts failed for ({lat}, {lon}): {last_error}"
    ) from last_error


def _parse_wind_response(data: dict) -> dict:
    """Extract wind speed and zone flags from the API response.

    The ASCE Hazard Tool API returns JSON with a structure like:
        {
            "requestInfo": { ... },
            "wind": {
                "windSpeed": <float>,
                "specialWindRegion": <bool>,
                "hurricaneProne": <bool>,
                ...
            }
        }

    NOTE: The exact field names may differ from this example. If the API
    returns different keys, update the field mappings below. You can inspect
    the actual response by running with --limit 1 and checking the logs.
    """
    wind = data.get("wind", data)

    # --- Wind speed ---
    # Try common field name patterns
    wind_speed = None
    for key in ("windSpeed", "windSpeedMph", "wind_speed", "vRef", "V"):
        if key in wind:
            wind_speed = wind[key]
            break

    # If the response nests values differently, try a flat search
    if wind_speed is None:
        for key, val in wind.items():
            if "wind" in key.lower() and "speed" in key.lower():
                wind_speed = val
                break

    # --- Hurricane-prone region ---
    is_hurricane = None
    for key in ("hurricaneProne", "isHurricaneProne", "hurricane_prone"):
        if key in wind:
            val = wind[key]
            is_hurricane = str(val).lower() in ("true", "yes", "1")
            break

    # --- Special wind region ---
    is_special = None
    for key in ("specialWindRegion", "isSpecialWindRegion", "special_wind_region"):
        if key in wind:
            val = wind[key]
            is_special = str(val).lower() in ("true", "yes", "1")
            break

    return {
        "wind_speed": wind_speed,
        "is_hurricane_zone": is_hurricane,
        "is_special_wind_zone": is_special,
        "_raw": wind,  # keep raw data for debugging
    }


def fetch_wind_data(
    lat: float,
    lon: float,
    token: str,
    session: requests.Session,
) -> dict:
    """Fetch both 300-year and 700-year wind speeds for a location.

    Makes two API calls: one for Risk Category I (300-yr MRI) and one for
    Risk Category II (700-yr MRI). Hurricane zone and special wind region
    flags are taken from the Risk Category II response.
    """
    # 300-year MRI (Risk Category I)
    resp_300 = _api_request(lat, lon, RISK_CATEGORY_300YR, token, session)
    parsed_300 = _parse_wind_response(resp_300)

    time.sleep(REQUEST_DELAY)

    # 700-year MRI (Risk Category II)
    resp_700 = _api_request(lat, lon, RISK_CATEGORY_700YR, token, session)
    parsed_700 = _parse_wind_response(resp_700)

    # Log raw response on first call for debugging field names
    if not hasattr(fetch_wind_data, "_logged_sample"):
        logger.info("Sample API response (300yr): %s", resp_300)
        logger.info("Sample API response (700yr): %s", resp_700)
        fetch_wind_data._logged_sample = True

    return {
        "wind_speed_300yr": parsed_300["wind_speed"],
        "wind_speed_700yr": parsed_700["wind_speed"],
        "is_hurricane_zone": parsed_700["is_hurricane_zone"],
        "is_special_wind_zone": parsed_700["is_special_wind_zone"],
    }


# ---------------------------------------------------------------------------
# Progress / resume support
# ---------------------------------------------------------------------------


def load_progress(progress_file: Path) -> set[tuple[float, float]]:
    """Load previously completed coordinates from the progress file."""
    completed = set()
    if progress_file.exists():
        with open(progress_file, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    completed.add((float(row["lat"]), float(row["lon"])))
                except (KeyError, ValueError):
                    continue
    return completed


def append_result(filepath: Path, row: dict, write_header: bool = False):
    """Append a single result row to the CSV file."""
    mode = "w" if write_header else "a"
    with open(filepath, mode, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Fetch ASCE 7-22 wind data for SEIA solar project locations."
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("ASCE_API_TOKEN", "957f4961-5bb3-4c93-a8c2-acfdcdb0b9f3"),
        help="ASCE Hazard Tool API token (or set ASCE_API_TOKEN env var).",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=EXCEL_FILE,
        help=f"Path to SEIA Solar Data Excel file (default: {EXCEL_FILE}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_CSV,
        help=f"Output CSV file path (default: {OUTPUT_CSV}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit to first N coordinates (useful for testing).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from a previous partial run, skipping already-fetched coordinates.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY,
        help=f"Delay in seconds between API requests (default: {REQUEST_DELAY}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read coordinates and print count without making API calls.",
    )
    args = parser.parse_args()

    # Validate token
    if not args.dry_run and not args.token:
        logger.error(
            "No API token provided. Set ASCE_API_TOKEN environment variable "
            "or pass --token YOUR_API_KEY."
        )
        sys.exit(1)

    # Read coordinates
    records = read_coordinates(args.input, limit=args.limit)
    if not records:
        logger.error("No valid coordinates found in the input file.")
        sys.exit(1)

    if args.dry_run:
        logger.info("Dry run: found %d coordinates. No API calls made.", len(records))
        # Print first 5 as a sample
        for r in records[:5]:
            logger.info("  %s, %s — %s (%s)", r["lat"], r["lon"], r["plant_name"], r["state"])
        sys.exit(0)

    # Resume support
    completed = set()
    write_header = True
    if args.resume:
        completed = load_progress(args.output)
        if completed:
            logger.info("Resuming: %d coordinates already completed.", len(completed))
            write_header = False
        else:
            logger.info("No previous progress found. Starting fresh.")

    # Initialize output file with header if starting fresh
    if write_header:
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()

    # Fetch wind data
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    total = len(records)
    success = 0
    errors = 0

    for i, record in enumerate(records, start=1):
        lat, lon = record["lat"], record["lon"]

        # Skip if already completed
        if (lat, lon) in completed:
            continue

        logger.info(
            "[%d/%d] Fetching wind data for (%s, %s) — %s",
            i, total, lat, lon, record.get("plant_name", ""),
        )

        try:
            wind = fetch_wind_data(lat, lon, args.token, session)
            result = {
                "lat": lat,
                "lon": lon,
                "plant_name": record.get("plant_name", ""),
                "state": record.get("state", ""),
                **wind,
            }
            append_result(args.output, result)
            success += 1
        except Exception:
            logger.exception("Failed to fetch data for (%s, %s)", lat, lon)
            # Write a row with empty wind data so we know it failed
            result = {
                "lat": lat,
                "lon": lon,
                "plant_name": record.get("plant_name", ""),
                "state": record.get("state", ""),
                "wind_speed_300yr": "ERROR",
                "wind_speed_700yr": "ERROR",
                "is_hurricane_zone": "ERROR",
                "is_special_wind_zone": "ERROR",
            }
            append_result(args.output, result)
            errors += 1

        # Rate limiting between locations
        if i < total:
            time.sleep(args.delay)

    logger.info(
        "Done. %d succeeded, %d failed out of %d total. Results: %s",
        success, errors, total, args.output,
    )


if __name__ == "__main__":
    main()
