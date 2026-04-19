#!/usr/bin/env python3
"""
NASA APOD Image Downloader

This script downloads images from NASA's Astronomy Picture of the Day (APOD) website,
with configurable options for date ranges, output directory, and more.

Usage:
    python apod_downloader.py [options]

Requirements:
    - requests
    - python-dateutil
    - pyyaml
    - rich
"""

import os
import sys
import json
import sqlite3
import threading
import argparse
import time
import concurrent.futures
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import piexif
import requests
import yaml
from dateutil import parser as date_parser
from PIL import Image
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

# First APOD was published on this date
FIRST_APOD_DATE = datetime(1995, 6, 16).date()

# NASA/GSFC is located in Greenbelt, Maryland — images are published on Eastern Time
GSFC_TZ = ZoneInfo("America/New_York")


def gsfc_today():
    """Return the current date at NASA's Goddard Space Flight Center (America/New_York)."""
    return datetime.now(GSFC_TZ).date()


class APODNotAvailableError(Exception):
    """Raised when the API reports no image is available for the requested date."""
    pass


def _get_config_dir() -> Path:
    """Return the platform-appropriate config directory for apod-downloader."""
    if sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    config_dir = base / 'apod-downloader'
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def _load_config() -> dict:
    """
    Load configuration from the platform config directory (config.yaml).

    Creates the file with a placeholder api_key if it does not exist.
    Returns an empty dict on parse errors.
    """
    config_path = _get_config_dir() / 'config.yaml'
    if not config_path.exists():
        config_path.write_text('api_key: your_api_key_here\n')
    try:
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


class APODCache:
    """
    SQLite-backed cache for APOD API responses.

    Stores JSON-serialised API responses keyed by date string (YYYY-MM-DD).
    The database lives alongside config.yaml in the platform config directory.
    Thread-safe for concurrent reads; writes are serialised through a single
    connection opened in check_same_thread=False mode with WAL journal.
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS apod_cache (
                date       TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                cached_at  TEXT NOT NULL
            )
        """)
        self._conn.commit()

    def get(self, date: str) -> dict | None:
        """Return cached APOD data for *date*, or None if not cached."""
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM apod_cache WHERE date = ?", (date,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def get_range(self, start: str, end: str) -> dict[str, dict]:
        """
        Return all cached entries in the date range [start, end] (inclusive).

        Returns a dict mapping date string → APOD data dict.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT date, data FROM apod_cache WHERE date BETWEEN ? AND ?",
                (start, end)
            ).fetchall()
        return {row[0]: json.loads(row[1]) for row in rows}

    def put(self, date: str, data: dict) -> None:
        """Insert or replace a single entry."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO apod_cache (date, data, cached_at) VALUES (?, ?, ?)",
                (date, json.dumps(data), now)
            )
            self._conn.commit()

    def put_many(self, entries: list[dict]) -> None:
        """Insert or replace a batch of APOD entries."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO apod_cache (date, data, cached_at) VALUES (?, ?, ?)",
                [(e['date'], json.dumps(e), now) for e in entries if 'date' in e]
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _stamp_file_times(path: Path, apod_date_str: str) -> None:
    """
    Set the file's atime, mtime, and birthtime to the APOD publication date.

    atime/mtime are set via os.utime() on all platforms. Birthtime is set
    where the OS permits: macOS via setattrlist(), Windows via SetFileTime().
    Linux does not expose a standard interface for setting birthtime, so only
    atime/mtime are updated there.
    """
    apod_dt = datetime.strptime(apod_date_str, "%Y-%m-%d")
    ts = apod_dt.timestamp()
    os.utime(path, (ts, ts))
    _set_birthtime(path, ts)


def _set_birthtime(path: Path, timestamp: float) -> None:
    """
    Attempt to set the file creation date (birthtime). Silently ignored on failure.

    macOS:   setattrlist() via ctypes
    Windows: SetFileTime() via ctypes.windll
    Linux:   no-op (birthtime is not writable via standard interfaces)
    """
    if sys.platform == 'darwin':
        _set_birthtime_darwin(path, timestamp)
    elif sys.platform == 'win32':
        _set_birthtime_win32(path, timestamp)


def _set_birthtime_darwin(path: Path, timestamp: float) -> None:
    """Set birthtime on macOS via setattrlist()."""
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)

        class _attrlist(ctypes.Structure):
            _fields_ = [
                ('bitmapcount', ctypes.c_ushort),
                ('reserved',    ctypes.c_ushort),
                ('commonattr',  ctypes.c_uint32),
                ('volattr',     ctypes.c_uint32),
                ('dirattr',     ctypes.c_uint32),
                ('fileattr',    ctypes.c_uint32),
                ('forkattr',    ctypes.c_uint32),
            ]

        class _timespec(ctypes.Structure):
            _fields_ = [('tv_sec', ctypes.c_long), ('tv_nsec', ctypes.c_long)]

        ATTR_BIT_MAP_COUNT = 5
        ATTR_CMN_CRTIME    = 0x00000200

        attrs = _attrlist(bitmapcount=ATTR_BIT_MAP_COUNT, commonattr=ATTR_CMN_CRTIME)
        buf   = _timespec(tv_sec=int(timestamp), tv_nsec=0)

        libc.setattrlist(
            str(path).encode(),
            ctypes.byref(attrs),
            ctypes.byref(buf),
            ctypes.sizeof(buf),
            ctypes.c_ulong(0),
        )
    except Exception:
        pass


def _set_birthtime_win32(path: Path, timestamp: float) -> None:
    """Set creation time on Windows via SetFileTime()."""
    try:
        import ctypes
        import ctypes.wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        # Convert Unix timestamp → Windows FILETIME (100-ns ticks since 1601-01-01)
        EPOCH_DIFF  = 116_444_736_000_000_000  # ticks between 1601 and 1970
        filetime_val = int(timestamp * 10_000_000) + EPOCH_DIFF

        class _FILETIME(ctypes.Structure):
            _fields_ = [
                ('dwLowDateTime',  ctypes.wintypes.DWORD),
                ('dwHighDateTime', ctypes.wintypes.DWORD),
            ]

        ft = _FILETIME(
            dwLowDateTime=filetime_val & 0xFFFF_FFFF,
            dwHighDateTime=(filetime_val >> 32) & 0xFFFF_FFFF,
        )

        GENERIC_WRITE        = 0x4000_0000
        FILE_SHARE_READ      = 0x0000_0001
        OPEN_EXISTING        = 3
        FILE_ATTRIBUTE_NORMAL = 0x0000_0080

        handle = kernel32.CreateFileW(
            str(path), GENERIC_WRITE, FILE_SHARE_READ,
            None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
        )
        if handle != ctypes.wintypes.HANDLE(-1).value:
            kernel32.SetFileTime(handle, ctypes.byref(ft), None, None)
            kernel32.CloseHandle(handle)
    except Exception:
        pass


def _format_summary(results):
    """Return a Rich-formatted download summary for a list of results."""
    successful = sum(1 for r in results if r['success'])
    skipped = sum(
        1 for r in results
        if not r['success'] and r.get('reason', '').startswith('Skipped:')
    )
    failed = len(results) - successful - skipped

    parts = [f"[bold]{successful}[/bold] downloaded"]
    if skipped:
        parts.append(f"[dim]{skipped} skipped (non-image)[/dim]")
    if failed:
        parts.append(f"[red]{failed} failed[/red]")
    return (
        "[green]✓[/green] "
        + "  ·  ".join(parts)
        + f"  [dim](of {len(results)} total)[/dim]"
    )


class APODDownloader:
    """Downloads images from NASA's Astronomy Picture of the Day (APOD) website."""

    BASE_URL = "https://api.nasa.gov/planetary/apod"

    def __init__(self, api_key=None, output_dir="apod_images",
                 max_workers=5, timeout=30, retry_attempts=3,
                 convert_to_png=False, use_cache=True):
        """
        Initialize the APOD Downloader.

        Args:
            api_key (str): NASA API key. Falls back to NASA_API_KEY env var,
                           then config.yaml, then DEMO_KEY.
            output_dir (str): Directory to save images.
            max_workers (int): Maximum number of concurrent downloads.
            timeout (int): Timeout for requests in seconds.
            retry_attempts (int): Number of retry attempts for failed requests.
            convert_to_png (bool): Convert non-JPEG/PNG images to PNG after download.
            use_cache (bool): Cache API responses in SQLite to avoid redundant calls.
        """
        config = _load_config()
        configured_key = config.get('api_key', '')
        # Treat the placeholder value as absent
        if configured_key == 'your_api_key_here':
            configured_key = ''

        self.api_key = (
            api_key
            or os.environ.get("NASA_API_KEY", "")
            or configured_key
            or "DEMO_KEY"
        )
        self.output_dir = Path(output_dir)
        self.max_workers = max_workers
        self.timeout = timeout
        self.retry_attempts = retry_attempts
        self.convert_to_png = convert_to_png
        self.session = requests.Session()
        self.console = Console()
        self._rate_limit = {'limit': None, 'remaining': None}

        self._cache: APODCache | None = (
            APODCache(_get_config_dir() / 'cache.db') if use_cache else None
        )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._clean_stale_parts()

    # ------------------------------------------------------------------
    # Startup helpers
    # ------------------------------------------------------------------

    def _clean_stale_parts(self):
        """Remove any .part files left behind by previously interrupted downloads."""
        stale = list(self.output_dir.glob('*.part'))
        for f in stale:
            f.unlink(missing_ok=True)
        if stale:
            self.console.print(
                f"[dim]Cleaned up {len(stale)} stale .part "
                f"{'file' if len(stale) == 1 else 'files'} from a previous run.[/dim]"
            )

    # ------------------------------------------------------------------
    # Rate limit helpers
    # ------------------------------------------------------------------

    def _update_rate_limit(self, response):
        """Cache X-RateLimit-Limit / X-RateLimit-Remaining from an API response."""
        limit = response.headers.get('X-RateLimit-Limit')
        remaining = response.headers.get('X-RateLimit-Remaining')
        if limit is not None:
            self._rate_limit['limit'] = int(limit)
        if remaining is not None:
            self._rate_limit['remaining'] = int(remaining)

    def _rate_limit_str(self):
        """Return a Rich-formatted rate limit badge, or empty string if not yet known."""
        if self._rate_limit['limit'] is None:
            return ""
        remaining = self._rate_limit['remaining']
        limit = self._rate_limit['limit']
        if remaining > limit * 0.5:
            color = "green"
        elif remaining > limit * 0.1:
            color = "yellow"
        else:
            color = "red"
        return f"  [dim]API: [{color}]{remaining}[/{color}]/{limit} calls remaining[/dim]"

    # ------------------------------------------------------------------
    # API fetch
    # ------------------------------------------------------------------

    def get_apod_data(self, date=None, start_date=None, end_date=None, count=None):
        """
        Get APOD data for a specific date, date range, or random entries.

        Args:
            date (str, optional): Single date in YYYY-MM-DD format.
            start_date (str, optional): Start date in YYYY-MM-DD format for a range.
            end_date (str, optional): End date in YYYY-MM-DD format for a range.
            count (int, optional): Number of random entries to return.

        Returns:
            dict or list: APOD data for the requested date(s).

        Raises:
            APODNotAvailableError: If the API reports no image exists for the date.
        """
        params = {'api_key': self.api_key}

        if date:
            params['date'] = date
        elif start_date and end_date:
            params['start_date'] = start_date
            params['end_date'] = end_date
        elif count:
            params['count'] = count

        if date:
            desc = f"Fetching APOD data for [bold]{date}[/bold]"
        elif start_date:
            desc = f"Fetching APOD data [bold]{start_date}[/bold] → [bold]{end_date}[/bold]"
        elif count:
            noun = "entry" if count == 1 else "entries"
            desc = f"Fetching [bold]{count}[/bold] random APOD {noun}"
        else:
            desc = "Fetching APOD data"

        with self.console.status(f"[cyan]{desc}...[/cyan]") as status:
            for attempt in range(self.retry_attempts):
                try:
                    response = self.session.get(
                        self.BASE_URL, params=params, timeout=self.timeout
                    )

                    if response.status_code == 429:
                        retry_after = int(response.headers.get('Retry-After', 60))
                        status.update(
                            f"[yellow]{desc} — rate limited, "
                            f"waiting {retry_after}s before retrying...[/yellow]"
                        )
                        time.sleep(retry_after)
                        status.update(f"[cyan]{desc}...[/cyan]")
                        continue

                    # 400/404: no image for this date — don't retry
                    if response.status_code in (400, 404):
                        msg = response.json().get('msg', 'No data available for this date')
                        raise APODNotAvailableError(msg)

                    response.raise_for_status()
                    self._update_rate_limit(response)
                    return response.json()

                except APODNotAvailableError:
                    raise  # propagate immediately, no retry

                except requests.exceptions.RequestException as e:
                    if attempt == self.retry_attempts - 1:
                        self.console.print(
                            f"[bold red]✗[/bold red] Failed after {self.retry_attempts} attempts: {e}"
                        )
                        return None
                    wait = 2 ** attempt
                    status.update(
                        f"[yellow]{desc} — attempt {attempt + 1} failed, "
                        f"retrying in {wait}s...[/yellow]"
                    )
                    time.sleep(wait)
                    status.update(f"[cyan]{desc}...[/cyan]")

    # ------------------------------------------------------------------
    # Image download
    # ------------------------------------------------------------------

    def download_image(self, url, filename, progress=None, task_id=None):
        """
        Download an image from a URL and save it to a file.

        Writes to a .part staging file and renames to the final path only on
        success, so an interrupted download is never mistaken for a complete one.

        Args:
            url (str): URL of the image to download.
            filename (Path): Path to save the image.
            progress (Progress, optional): Rich Progress instance for byte tracking.
            task_id: Task ID within the Progress instance.

        Returns:
            bool: True if download was successful, False otherwise.
        """
        if filename.exists():
            return True

        part = filename.with_suffix(filename.suffix + '.part')
        part.unlink(missing_ok=True)

        for attempt in range(self.retry_attempts):
            try:
                response = self.session.get(url, stream=True, timeout=self.timeout)

                if response.status_code == 404:
                    part.unlink(missing_ok=True)
                    self.console.print(
                        f"[yellow]⚠ Image not found (404): {url}[/yellow]"
                    )
                    return False

                response.raise_for_status()

                total_size = int(response.headers.get('content-length', 0)) or None
                if progress is not None and task_id is not None:
                    progress.update(task_id, total=total_size)

                with open(part, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            if progress is not None and task_id is not None:
                                progress.advance(task_id, len(chunk))

                part.rename(filename)
                return True

            except requests.exceptions.RequestException as e:
                part.unlink(missing_ok=True)
                if attempt == self.retry_attempts - 1:
                    self.console.print(
                        f"[bold red]✗[/bold red] Download failed after "
                        f"{self.retry_attempts} attempts: {e}"
                    )
                    return False
                wait = 2 ** attempt
                self.console.print(
                    f"[yellow]Attempt {attempt + 1} failed, retrying in {wait}s...[/yellow]"
                )
                time.sleep(wait)

    def process_apod_entry(self, entry, image_progress=None):
        """
        Process a single APOD entry and download its image.

        Args:
            entry (dict): APOD data entry.
            image_progress (Progress, optional): Rich Progress for per-file byte tracking.

        Returns:
            dict: Result of the download operation.
        """
        result = {
            'date': entry.get('date'),
            'title': entry.get('title'),
            'success': False
        }

        if entry.get('media_type') != 'image':
            result['reason'] = f"Skipped: {entry.get('media_type')}"
            return result

        image_url = entry.get('hdurl') or entry.get('url')
        if not image_url:
            result['reason'] = "No image URL found"
            return result

        parsed_url = urlparse(image_url)
        _, ext = os.path.splitext(parsed_url.path)
        if not ext:
            ext = '.jpg'

        safe_title = "".join(
            c if c.isalnum() or c in ' -_' else '_'
            for c in entry.get('title', '')
        )
        safe_title = safe_title.replace(' ', '_')
        filename = self.output_dir / f"{entry.get('date')}_{safe_title}{ext}"

        # When --convert-to-png is active, non-JPEG/PNG files land on disk as .png.
        # Check for that final path so we don't re-download an already-converted file.
        _jpeg_exts = {'.jpg', '.jpeg'}
        _png_exts  = {'.png'}
        if self.convert_to_png and ext.lower() not in _jpeg_exts | _png_exts:
            final_filename = filename.with_suffix('.png')
        else:
            final_filename = filename

        # Short-circuit if already downloaded — no progress task needed
        if final_filename.exists():
            result['success'] = True
            result['filename'] = str(final_filename)
            return result

        task_id = None
        if image_progress is not None:
            task_id = image_progress.add_task(filename.name, total=None)

        success = self.download_image(image_url, filename, image_progress, task_id)

        if image_progress is not None and task_id is not None:
            image_progress.remove_task(task_id)

        if success:
            filename = self._apply_image_metadata(filename, entry.get('date', ''))
            result['success'] = True
            result['filename'] = str(filename)
        else:
            result['success'] = False
            result['reason'] = "Download failed"

        return result

    # ------------------------------------------------------------------
    # Progress factories
    # ------------------------------------------------------------------

    def _single_file_progress(self):
        """Rich Progress configured for single-file byte-level tracking."""
        return Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}[/cyan]"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=self.console,
        )

    # ------------------------------------------------------------------
    # Image post-processing
    # ------------------------------------------------------------------

    def _apply_image_metadata(self, filename: Path, apod_date_str: str) -> Path:
        """
        Ensure the image is JPEG or PNG and carries the APOD publication date
        in its EXIF metadata.

        Non-JPEG/PNG files (GIF, TIFF, WebP, …) are converted to PNG.
        For JPEG, EXIF is patched in-place via piexif.insert() to avoid
        re-encoding and quality loss. For PNG, the file is re-saved with
        an eXIf chunk. Existing EXIF data is preserved for JPEG; only the
        date fields are overwritten.

        Args:
            filename: Path to the successfully downloaded image.
            apod_date_str: APOD publication date string (YYYY-MM-DD).

        Returns:
            Path: Final file path (differs from input if format conversion occurred).
        """
        try:
            img = Image.open(filename)
        except Exception as e:
            self.console.print(
                f"[yellow]⚠ Could not open {filename.name} for metadata: {e}[/yellow]"
            )
            return filename

        fmt = img.format  # 'JPEG', 'PNG', 'GIF', 'TIFF', 'WEBP', …
        apod_dt = datetime.strptime(apod_date_str, "%Y-%m-%d")
        exif_date = apod_dt.strftime("%Y:%m:%d 00:00:00").encode()
        needs_conversion = self.convert_to_png and fmt not in ('JPEG', 'PNG')
        final_path = filename.with_suffix('.png') if needs_conversion else filename

        # For JPEG, preserve any existing EXIF (camera data, etc.) and only
        # overwrite the date fields. For all other formats start fresh.
        exif_dict: dict = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}, "1st": {}}
        if fmt == 'JPEG':
            try:
                raw = img.info.get('exif', b'')
                if raw:
                    exif_dict = piexif.load(raw)
            except Exception:
                pass  # malformed existing EXIF — fall back to clean dict

        exif_dict.setdefault("0th", {})[piexif.ImageIFD.DateTime] = exif_date
        exif_dict.setdefault("Exif", {})[piexif.ExifIFD.DateTimeOriginal] = exif_date
        exif_dict.setdefault("Exif", {})[piexif.ExifIFD.DateTimeDigitized] = exif_date

        try:
            exif_bytes = piexif.dump(exif_dict)
        except Exception:
            # Existing EXIF may contain values piexif can't serialise — retry clean
            exif_dict = {
                "0th": {piexif.ImageIFD.DateTime: exif_date},
                "Exif": {
                    piexif.ExifIFD.DateTimeOriginal: exif_date,
                    piexif.ExifIFD.DateTimeDigitized: exif_date,
                },
                "GPS": {}, "Interop": {}, "1st": {},
            }
            exif_bytes = piexif.dump(exif_dict)

        try:
            if needs_conversion:
                # Normalise palette/unusual modes before saving as PNG
                if img.mode in ('P', 'PA'):
                    img = img.convert('RGBA')
                elif img.mode not in ('RGB', 'RGBA', 'L', 'LA', '1'):
                    img = img.convert('RGB')
                img.save(str(final_path), 'PNG', exif=exif_bytes)
                img.close()
                filename.unlink()
                self.console.print(
                    f"[dim]  Converted {filename.name} ({fmt}) → {final_path.name}[/dim]"
                )
            elif fmt == 'JPEG':
                img.close()
                piexif.insert(exif_bytes, str(filename))  # in-place, no re-encode
            else:  # PNG
                img.save(str(final_path), 'PNG', exif=exif_bytes)
                img.close()
        except Exception as e:
            self.console.print(
                f"[yellow]⚠ Could not write metadata for {filename.name}: {e}[/yellow]"
            )
            try:
                img.close()
            except Exception:
                pass
            return filename  # return original if conversion/EXIF write failed

        _stamp_file_times(final_path, apod_date_str)
        return final_path

    # ------------------------------------------------------------------
    # Download orchestration
    # ------------------------------------------------------------------

    def _download_entries(self, entries, save_metadata):
        """
        Download a list of APOD entries concurrently with a batch progress bar.

        Args:
            entries (list): List of APOD data dicts.
            save_metadata (bool): Whether to save metadata as JSON files.

        Returns:
            list: Results of download operations.
        """
        results = []

        with Progress(
            SpinnerColumn(),
            MofNCompleteColumn(),
            BarColumn(),
            TextColumn("[dim]{task.description}[/dim]"),
            console=self.console,
        ) as progress:
            overall = progress.add_task("", total=len(entries))

            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_entry = {
                    executor.submit(self.process_apod_entry, entry): entry
                    for entry in entries
                }
                for future in concurrent.futures.as_completed(future_to_entry):
                    entry = future_to_entry[future]
                    try:
                        result = future.result()
                        results.append(result)
                        progress.update(
                            overall,
                            advance=1,
                            description=(
                                f"{entry.get('date')}  {entry.get('title', '')[:50]}"
                            ),
                        )
                        if save_metadata and result.get('success'):
                            metadata_file = Path(result['filename']).with_suffix('.json')
                            if not metadata_file.exists():
                                with open(metadata_file, 'w') as f:
                                    json.dump(entry, f, indent=2)
                                _stamp_file_times(metadata_file, entry.get('date', ''))
                    except Exception as e:
                        self.console.print(
                            f"[bold red]✗[/bold red] Error on {entry.get('date')}: {e}"
                        )
                        results.append({
                            'date': entry.get('date'),
                            'title': entry.get('title'),
                            'success': False,
                            'reason': str(e)
                        })
                        progress.update(overall, advance=1)

        return results

    def download_date_range(self, start_date, end_date, save_metadata=True):
        """
        Download APOD images for a date range, chunked into 100-day batches.

        Args:
            start_date (str): Start date in YYYY-MM-DD format.
            end_date (str): End date in YYYY-MM-DD format.
            save_metadata (bool): Whether to save metadata as JSON files.

        Returns:
            list: Results of download operations.
        """
        start_date_obj = date_parser.parse(start_date).date()
        end_date_obj = date_parser.parse(end_date).date()

        results = []
        current_start = start_date_obj

        while current_start <= end_date_obj:
            current_end = min(current_start + timedelta(days=99), end_date_obj)
            chunk_results = self._download_date_chunk(
                current_start.strftime("%Y-%m-%d"),
                current_end.strftime("%Y-%m-%d"),
                save_metadata
            )
            results.extend(chunk_results)
            current_start = current_end + timedelta(days=1)

        return results

    def _download_date_chunk(self, start_date, end_date, save_metadata):
        """
        Fetch and download a single 100-day chunk of APOD entries.

        Checks the local cache before making an API call. If every date in the
        range is already cached, the API call is skipped entirely.

        Args:
            start_date (str): Start date in YYYY-MM-DD format.
            end_date (str): End date in YYYY-MM-DD format.
            save_metadata (bool): Whether to save metadata as JSON files.

        Returns:
            list: Results of download operations.
        """
        if self._cache is not None:
            start_obj = date_parser.parse(start_date).date()
            end_obj = date_parser.parse(end_date).date()
            expected = set()
            d = start_obj
            while d <= end_obj:
                expected.add(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)

            cached = self._cache.get_range(start_date, end_date)
            if expected == set(cached.keys()):
                noun = "entry" if len(cached) == 1 else "entries"
                self.console.print(
                    f"[green]✓[/green] [bold]{len(cached)}[/bold] {noun} fetched"
                    f"  [dim](cached)[/dim]"
                )
                return self._download_entries(list(cached.values()), save_metadata)

        data = self.get_apod_data(start_date=start_date, end_date=end_date)

        if not data:
            self.console.print("[bold red]✗[/bold red] No data returned from API")
            return []

        if isinstance(data, dict):
            data = [data]

        noun = "entry" if len(data) == 1 else "entries"
        self.console.print(
            f"[green]✓[/green] [bold]{len(data)}[/bold] {noun} fetched"
            + self._rate_limit_str()
        )

        if self._cache is not None:
            self._cache.put_many(data)

        return self._download_entries(data, save_metadata)

    def download_single_date(self, date, save_metadata=True):
        """
        Download APOD image for a single date.

        Args:
            date (str): Date in YYYY-MM-DD format.
            save_metadata (bool): Whether to save metadata as JSON files.

        Returns:
            dict: Result of the download operation.
        """
        data = None
        from_cache = False

        if self._cache is not None:
            cached = self._cache.get(date)
            if cached is not None:
                data = cached
                from_cache = True

        if not from_cache:
            try:
                data = self.get_apod_data(date=date)
            except APODNotAvailableError as e:
                return {'date': date, 'success': False, 'reason': 'not_available', 'detail': str(e)}

            if not data:
                return {'date': date, 'success': False, 'reason': 'api_error'}

            if self._cache is not None:
                self._cache.put(date, data)

        suffix = "  [dim](cached)[/dim]" if from_cache else self._rate_limit_str()
        self.console.print(
            f"[green]✓[/green] [bold]{data.get('title', date)}[/bold]" + suffix
        )

        with self._single_file_progress() as progress:
            result = self.process_apod_entry(data, image_progress=progress)

        if save_metadata and result.get('success'):
            metadata_file = Path(result['filename']).with_suffix('.json')
            if not metadata_file.exists():
                with open(metadata_file, 'w') as f:
                    json.dump(data, f, indent=2)
                _stamp_file_times(metadata_file, data.get('date', ''))

        return result

    def download_latest(self, save_metadata=True):
        """
        Download the latest APOD image.

        Uses NASA/GSFC's local date (America/New_York). If today's image is not
        yet available (APODNotAvailableError), falls back to yesterday. Other
        failures (network errors, API errors) are returned as-is without fallback.

        Args:
            save_metadata (bool): Whether to save metadata as JSON files.

        Returns:
            dict: Result of the download operation.
        """
        today = gsfc_today()
        result = self.download_single_date(today.strftime("%Y-%m-%d"), save_metadata)

        if result.get('reason') == 'not_available':
            yesterday = today - timedelta(days=1)
            self.console.print(
                f"[yellow]Today's image ({today}, America/New_York) not yet available — "
                f"NASA publishes on Eastern Time. Falling back to {yesterday}...[/yellow]"
            )
            result = self.download_single_date(yesterday.strftime("%Y-%m-%d"), save_metadata)

        return result

    def download_random(self, count=1, save_metadata=True):
        """
        Download one or more random APOD images using the API's count parameter.

        Args:
            count (int): Number of random images to download.
            save_metadata (bool): Whether to save metadata as JSON files.

        Returns:
            dict: Single result when count=1.
            list: List of results when count>1.
        """
        data = self.get_apod_data(count=count)

        if not data:
            return {'success': False, 'reason': 'api_error'} if count == 1 else []

        entries = data if isinstance(data, list) else [data]

        if count == 1:
            entry = entries[0]
            self.console.print(
                f"[green]✓[/green] [bold]{entry.get('date')}[/bold] — {entry.get('title', '')}"
                + self._rate_limit_str()
            )
            with self._single_file_progress() as progress:
                result = self.process_apod_entry(entry, image_progress=progress)
            if save_metadata and result.get('success'):
                metadata_file = Path(result['filename']).with_suffix('.json')
                if not metadata_file.exists():
                    with open(metadata_file, 'w') as f:
                        json.dump(entry, f, indent=2)
                    _stamp_file_times(metadata_file, entry.get('date', ''))
            return result

        # count > 1: use batch progress
        noun = "entries" if count > 1 else "entry"
        self.console.print(
            f"[green]✓[/green] [bold]{len(entries)}[/bold] random {noun} fetched"
            + self._rate_limit_str()
        )
        return self._download_entries(entries, save_metadata)

    def download_all(self, save_metadata=True):
        """
        Download the complete APOD archive from the first entry to today.

        Args:
            save_metadata (bool): Whether to save metadata as JSON files.

        Returns:
            list: Results of download operations.
        """
        today = gsfc_today()
        start = FIRST_APOD_DATE.strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        self.console.print(
            f"[bold]Downloading complete APOD archive[/bold]  "
            f"[dim]{start}[/dim] → [dim]{end}[/dim]"
        )
        return self.download_date_range(start, end, save_metadata)

    def check_rate_limit(self):
        """
        Make a minimal API call to fetch the current rate limit status and display it.

        Requests a single well-known historical date (the first APOD) so the
        response payload is as small as possible. The cache is deliberately
        bypassed because we need a live response to get fresh header values.

        If the quota is currently exhausted (429) the remaining wait time is
        shown instead.
        """
        params = {
            'api_key': self.api_key,
            'date': FIRST_APOD_DATE.strftime("%Y-%m-%d"),
        }

        with self.console.status("[cyan]Checking API rate limit...[/cyan]"):
            try:
                response = self.session.get(
                    self.BASE_URL, params=params, timeout=self.timeout
                )
            except requests.exceptions.RequestException as e:
                self.console.print(f"[bold red]✗[/bold red] Request failed: {e}")
                return

        if response.status_code == 429:
            retry_after = int(response.headers.get('Retry-After', 0))
            h, m = divmod(retry_after // 60, 60)
            wait_str = f"{h}h {m}m" if h else f"{m}m"
            self.console.print(
                f"[bold red]Rate limited[/bold red]  "
                f"[dim]quota exhausted — resets in ~{wait_str} "
                f"({retry_after:,} seconds)[/dim]"
            )
            return

        self._update_rate_limit(response)
        limit     = self._rate_limit['limit']
        remaining = self._rate_limit['remaining']

        if limit is None:
            self.console.print("[yellow]⚠ Rate limit headers not present in response.[/yellow]")
            return

        used      = limit - remaining
        pct_used  = used / limit
        bar_width = 40
        filled    = round(bar_width * pct_used)
        empty     = bar_width - filled

        if pct_used < 0.5:
            bar_color = "green"
        elif pct_used < 0.9:
            bar_color = "yellow"
        else:
            bar_color = "red"

        bar = f"[{bar_color}]{'█' * filled}[/{bar_color}][dim]{'░' * empty}[/dim]"
        self.console.print(
            f"NASA API  ·  "
            f"used: [bold]{used:,}[/bold]/{limit:,}  "
            f"{bar}  "
            f"remaining: [{bar_color}][bold]{remaining:,}[/bold][/{bar_color}]"
        )


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Download NASA Astronomy Picture of the Day (APOD) images.'
    )

    date_group = parser.add_mutually_exclusive_group()
    date_group.add_argument('--date', help='Download image for specific date (YYYY-MM-DD)')
    date_group.add_argument('--start-date', help='Start date for range (YYYY-MM-DD)')
    date_group.add_argument('--latest', action='store_true', help='Download only the latest image')
    date_group.add_argument('--random', action='store_true', help='Download random image(s)')
    date_group.add_argument('--all', action='store_true', help='Download the complete APOD archive')

    parser.add_argument('--status', action='store_true',
                        help='Show current NASA API rate limit usage and exit')

    parser.add_argument('--end-date',
                        help='End date for range (YYYY-MM-DD); defaults to today when omitted)')
    parser.add_argument('--last-days', type=int, help='Download images from the last N days')
    parser.add_argument('--count', type=int, default=1,
                        help='Number of random images to download (use with --random, default: 1)')

    parser.add_argument('--output-dir', default='apod_images',
                        help='Directory to save images (default: apod_images)')
    parser.add_argument('--no-metadata', action='store_true',
                        help='Do not save metadata JSON files')
    parser.add_argument('--convert-to-png', action='store_true',
                        help='Convert non-JPEG/PNG images (GIF, TIFF, WebP, …) to PNG')
    parser.add_argument('--no-cache', action='store_true',
                        help='Bypass the local metadata cache and always fetch from the API')

    parser.add_argument('--api-key', default=None,
                        help='NASA API key (overrides config.yaml and NASA_API_KEY env var)')

    parser.add_argument('--max-workers', type=int, default=5,
                        help='Maximum number of concurrent downloads (default: 5)')
    parser.add_argument('--timeout', type=int, default=30,
                        help='Request timeout in seconds (default: 30)')
    parser.add_argument('--retry-attempts', type=int, default=3,
                        help='Number of retry attempts (default: 3)')

    return parser.parse_args()


def main():
    """Main function to run the APOD downloader."""
    args = parse_arguments()

    downloader = APODDownloader(
        api_key=args.api_key,
        output_dir=args.output_dir,
        max_workers=args.max_workers,
        timeout=args.timeout,
        retry_attempts=args.retry_attempts,
        convert_to_png=args.convert_to_png,
        use_cache=not args.no_cache,
    )

    console = downloader.console
    save_metadata = not args.no_metadata

    if args.status:
        downloader.check_rate_limit()
        return

    if args.date:
        result = downloader.download_single_date(args.date, save_metadata)
        if result['success']:
            console.print(f"[green]✓[/green] Saved to [bold]{result['filename']}[/bold]")
        else:
            console.print(f"[bold red]✗[/bold red] {args.date}: {result.get('reason')}")

    elif args.start_date:
        end_date = args.end_date or gsfc_today().strftime("%Y-%m-%d")
        results = downloader.download_date_range(args.start_date, end_date, save_metadata)
        console.print("\n" + _format_summary(results))

    elif args.last_days:
        end_date = gsfc_today()
        start_date = end_date - timedelta(days=args.last_days - 1)
        results = downloader.download_date_range(
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"),
            save_metadata
        )
        console.print("\n" + _format_summary(results))

    elif args.latest:
        result = downloader.download_latest(save_metadata)
        if result['success']:
            console.print(f"[green]✓[/green] Saved to [bold]{result['filename']}[/bold]")
        else:
            console.print(f"[bold red]✗[/bold red] {result.get('reason')}")

    elif args.random:
        if args.count > 1:
            results = downloader.download_random(count=args.count, save_metadata=save_metadata)
            console.print("\n" + _format_summary(results))
        else:
            result = downloader.download_random(count=1, save_metadata=save_metadata)
            if result['success']:
                console.print(f"[green]✓[/green] Saved to [bold]{result['filename']}[/bold]")
            else:
                console.print(f"[bold red]✗[/bold red] {result.get('reason')}")

    elif getattr(args, 'all', False):
        results = downloader.download_all(save_metadata)
        console.print("\n" + _format_summary(results))

    else:
        result = downloader.download_latest(save_metadata)
        if result['success']:
            console.print(f"[green]✓[/green] Saved to [bold]{result['filename']}[/bold]")
        else:
            console.print(f"[bold red]✗[/bold red] {result.get('reason')}")


if __name__ == "__main__":
    main()
