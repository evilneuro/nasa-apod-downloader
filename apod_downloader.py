#!/usr/bin/env python3
"""
NASA APOD Image Downloader

This script downloads images from NASA's Astronomy Picture of the Day (APOD) website,
with configurable options for date ranges, output directory, and more.

Usage:
    python apod_downloader.py [options]

Requirements:
    - requests
    - tqdm
    - python-dateutil
    - python-dotenv
"""

import os
import sys
import json
import random
import argparse
import time
import concurrent.futures
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from dateutil import parser as date_parser
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

# First APOD was published on this date
FIRST_APOD_DATE = datetime(1995, 6, 16).date()

# NASA/GSFC is located in Greenbelt, Maryland — images are published on Eastern Time
GSFC_TZ = ZoneInfo("America/New_York")


def gsfc_today():
    """Return the current date at NASA's Goddard Space Flight Center (America/New_York)."""
    return datetime.now(GSFC_TZ).date()


class APODDownloader:
    """Downloads images from NASA's Astronomy Picture of the Day (APOD) website."""

    BASE_URL = "https://api.nasa.gov/planetary/apod"

    def __init__(self, api_key=None, output_dir="apod_images",
                 max_workers=5, timeout=30, retry_attempts=3):
        """
        Initialize the APOD Downloader.

        Args:
            api_key (str): NASA API key. Falls back to NASA_API_KEY env var, then DEMO_KEY.
            output_dir (str): Directory to save images.
            max_workers (int): Maximum number of concurrent downloads.
            timeout (int): Timeout for requests in seconds.
            retry_attempts (int): Number of retry attempts for failed requests.
        """
        self.api_key = api_key or os.environ.get("NASA_API_KEY", "DEMO_KEY")
        self.output_dir = Path(output_dir)
        self.max_workers = max_workers
        self.timeout = timeout
        self.retry_attempts = retry_attempts
        self.session = requests.Session()

        self.output_dir.mkdir(parents=True, exist_ok=True)

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
        """
        params = {'api_key': self.api_key}

        if date:
            params['date'] = date
        elif start_date and end_date:
            params['start_date'] = start_date
            params['end_date'] = end_date
        elif count:
            params['count'] = count

        for attempt in range(self.retry_attempts):
            try:
                response = self.session.get(
                    self.BASE_URL,
                    params=params,
                    timeout=self.timeout
                )

                if response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', 60))
                    print(f"Rate limited. Waiting {retry_after}s before retrying...")
                    time.sleep(retry_after)
                    continue

                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                if attempt == self.retry_attempts - 1:
                    print(f"Failed to fetch APOD data after {self.retry_attempts} attempts: {e}")
                    return None
                wait = 2 ** attempt
                print(f"Attempt {attempt + 1} failed, retrying in {wait}s...")
                time.sleep(wait)

    def download_image(self, url, filename):
        """
        Download an image from a URL and save it to a file.

        Args:
            url (str): URL of the image to download.
            filename (Path): Path to save the image.

        Returns:
            bool: True if download was successful, False otherwise.
        """
        if filename.exists():
            return True

        for attempt in range(self.retry_attempts):
            try:
                response = self.session.get(url, stream=True, timeout=self.timeout)
                response.raise_for_status()

                with open(filename, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                return True
            except requests.exceptions.RequestException as e:
                if attempt == self.retry_attempts - 1:
                    print(f"Failed to download {url} after {self.retry_attempts} attempts: {e}")
                    return False
                wait = 2 ** attempt
                print(f"Attempt {attempt + 1} failed, retrying in {wait}s...")
                time.sleep(wait)

    def process_apod_entry(self, entry):
        """
        Process a single APOD entry and download its image.

        Args:
            entry (dict): APOD data entry.

        Returns:
            dict: Result of the download operation.
        """
        result = {
            'date': entry.get('date'),
            'title': entry.get('title'),
            'success': False
        }

        if entry.get('media_type') != 'image':
            result['reason'] = f"Skipped media type: {entry.get('media_type')}"
            return result

        image_url = entry.get('hdurl') or entry.get('url')
        if not image_url:
            result['reason'] = "No image URL found"
            return result

        parsed_url = urlparse(image_url)
        _, ext = os.path.splitext(parsed_url.path)
        if not ext:
            ext = '.jpg'

        safe_title = "".join(c if c.isalnum() or c in ' -_' else '_' for c in entry.get('title', ''))
        safe_title = safe_title.replace(' ', '_')
        filename = self.output_dir / f"{entry.get('date')}_{safe_title}{ext}"

        success = self.download_image(image_url, filename)
        result['success'] = success

        if success:
            result['filename'] = str(filename)
        else:
            result['reason'] = "Download failed"

        return result

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
        Download APOD images for a chunk of dates (up to 100 days).

        Args:
            start_date (str): Start date in YYYY-MM-DD format.
            end_date (str): End date in YYYY-MM-DD format.
            save_metadata (bool): Whether to save metadata as JSON files.

        Returns:
            list: Results of download operations.
        """
        print(f"Fetching APOD data from {start_date} to {end_date}...")
        data = self.get_apod_data(start_date=start_date, end_date=end_date)

        if not data:
            print("No data returned from API")
            return []

        if isinstance(data, dict):
            data = [data]

        print(f"Found {len(data)} APOD entries. Starting downloads...")

        results = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_entry = {executor.submit(self.process_apod_entry, entry): entry for entry in data}

            for future in tqdm(concurrent.futures.as_completed(future_to_entry), total=len(data), desc="Downloading"):
                entry = future_to_entry[future]
                try:
                    result = future.result()
                    results.append(result)

                    if save_metadata and result.get('success'):
                        metadata_file = Path(result['filename']).with_suffix('.json')
                        with open(metadata_file, 'w') as f:
                            json.dump(entry, f, indent=2)

                except Exception as e:
                    print(f"Error processing {entry.get('date')}: {e}")
                    results.append({
                        'date': entry.get('date'),
                        'title': entry.get('title'),
                        'success': False,
                        'reason': str(e)
                    })

        return results

    def download_single_date(self, date, save_metadata=True):
        """
        Download APOD image for a single date.

        Args:
            date (str): Date in YYYY-MM-DD format.
            save_metadata (bool): Whether to save metadata as JSON files.

        Returns:
            dict: Result of the download operation.
        """
        print(f"Fetching APOD data for {date}...")
        data = self.get_apod_data(date=date)

        if not data:
            print("No data returned from API")
            return {'date': date, 'success': False, 'reason': "No data returned from API"}

        result = self.process_apod_entry(data)

        if save_metadata and result.get('success'):
            metadata_file = Path(result['filename']).with_suffix('.json')
            with open(metadata_file, 'w') as f:
                json.dump(data, f, indent=2)

        return result

    def download_latest(self, save_metadata=True):
        """
        Download the latest APOD image.

        Uses NASA/GSFC's local date (America/New_York) since images are published
        on Eastern Time. If today's image is not yet available, falls back to
        yesterday's entry.

        Args:
            save_metadata (bool): Whether to save metadata as JSON files.

        Returns:
            dict: Result of the download operation.
        """
        today = gsfc_today()
        result = self.download_single_date(today.strftime("%Y-%m-%d"), save_metadata)

        if not result['success']:
            yesterday = today - timedelta(days=1)
            print(
                f"Note: today's image ({today}, America/New_York) is not yet available — "
                f"NASA publishes on Eastern Time. Falling back to {yesterday}..."
            )
            result = self.download_single_date(yesterday.strftime("%Y-%m-%d"), save_metadata)

        return result

    def download_random(self, save_metadata=True):
        """
        Download a single random APOD image using the API's count parameter.

        Args:
            save_metadata (bool): Whether to save metadata as JSON files.

        Returns:
            dict: Result of the download operation.
        """
        print("Fetching a random APOD entry...")
        data = self.get_apod_data(count=1)

        if not data:
            print("No data returned from API")
            return {'success': False, 'reason': "No data returned from API"}

        entry = data[0] if isinstance(data, list) else data
        print(f"Selected random date: {entry.get('date')}")

        result = self.process_apod_entry(entry)

        if save_metadata and result.get('success'):
            metadata_file = Path(result['filename']).with_suffix('.json')
            with open(metadata_file, 'w') as f:
                json.dump(entry, f, indent=2)

        return result

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
        print(f"Downloading complete APOD archive from {start} to {end}...")
        return self.download_date_range(start, end, save_metadata)


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Download NASA Astronomy Picture of the Day (APOD) images.')

    # Date selection options
    date_group = parser.add_mutually_exclusive_group()
    date_group.add_argument('--date', help='Download image for specific date (YYYY-MM-DD)')
    date_group.add_argument('--start-date', help='Start date for range (YYYY-MM-DD)')
    date_group.add_argument('--latest', action='store_true', help='Download only the latest image')
    date_group.add_argument('--random', action='store_true', help='Download a random image from the archive')
    date_group.add_argument('--all', action='store_true', help='Download the complete APOD archive')

    parser.add_argument('--end-date', help='End date for range (YYYY-MM-DD, requires --start-date)')
    parser.add_argument('--last-days', type=int, help='Download images from the last N days')

    # Output options
    parser.add_argument('--output-dir', default='apod_images', help='Directory to save images (default: apod_images)')
    parser.add_argument('--no-metadata', action='store_true', help='Do not save metadata JSON files')

    # API options
    parser.add_argument('--api-key', default=None,
                        help='NASA API key (overrides NASA_API_KEY env var; default: DEMO_KEY)')

    # Performance options
    parser.add_argument('--max-workers', type=int, default=5, help='Maximum number of concurrent downloads (default: 5)')
    parser.add_argument('--timeout', type=int, default=30, help='Request timeout in seconds (default: 30)')
    parser.add_argument('--retry-attempts', type=int, default=3, help='Number of retry attempts (default: 3)')

    return parser.parse_args()


def main():
    """Main function to run the APOD downloader."""
    args = parse_arguments()

    downloader = APODDownloader(
        api_key=args.api_key,
        output_dir=args.output_dir,
        max_workers=args.max_workers,
        timeout=args.timeout,
        retry_attempts=args.retry_attempts
    )

    save_metadata = not args.no_metadata

    if args.date:
        result = downloader.download_single_date(args.date, save_metadata)
        if result['success']:
            print(f"Successfully downloaded image for {args.date} to {result['filename']}")
        else:
            print(f"Failed to download image for {args.date}: {result.get('reason')}")

    elif args.start_date:
        if not args.end_date:
            print("Error: --end-date is required when using --start-date")
            sys.exit(1)

        results = downloader.download_date_range(args.start_date, args.end_date, save_metadata)
        successful = sum(1 for r in results if r['success'])
        print(f"\nDownload complete. Successfully downloaded {successful} of {len(results)} images.")

    elif args.last_days:
        end_date = gsfc_today()
        start_date = end_date - timedelta(days=args.last_days - 1)

        results = downloader.download_date_range(
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"),
            save_metadata
        )
        successful = sum(1 for r in results if r['success'])
        print(f"\nDownload complete. Successfully downloaded {successful} of {len(results)} images.")

    elif args.latest:
        result = downloader.download_latest(save_metadata)
        if result['success']:
            print(f"Successfully downloaded latest image to {result['filename']}")
        else:
            print(f"Failed to download latest image: {result.get('reason')}")

    elif args.random:
        result = downloader.download_random(save_metadata)
        if result['success']:
            print(f"Successfully downloaded random image from {result.get('date')} to {result['filename']}")
        else:
            print(f"Failed to download random image: {result.get('reason')}")

    elif getattr(args, 'all', False):
        results = downloader.download_all(save_metadata)
        successful = sum(1 for r in results if r['success'])
        print(f"\nDownload complete. Successfully downloaded {successful} of {len(results)} images.")

    else:
        result = downloader.download_latest(save_metadata)
        if result['success']:
            print(f"Successfully downloaded latest image to {result['filename']}")
        else:
            print(f"Failed to download latest image: {result.get('reason')}")


if __name__ == "__main__":
    main()
