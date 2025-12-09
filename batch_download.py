#!/usr/bin/env python3
"""
Batch download Majsoul game records

Read paipu IDs from CSV file and download all records to output directory

Usage:
    python batch_download.py <csv_file>
    python batch_download.py <csv_file> --delay 2  # Set request interval (seconds)
    python batch_download.py <csv_file> --skip-existing  # Skip already downloaded
"""

import asyncio
import csv
import sys
import argparse
import time
from pathlib import Path

# Import functions from paipu_dl
from paipu_dl import (
    load_credentials,
    download_record,
    OUTPUT_DIR,
)


def load_paipu_ids_from_csv(csv_path: str) -> list[str]:
    """
    Read all paipu IDs from CSV file
    """
    paipu_ids = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Try common column names for paipu link
            paipu_id = row.get('牌谱链接', '') or row.get('paipu_id', '') or row.get('uuid', '')
            paipu_id = paipu_id.strip()
            if paipu_id:
                paipu_ids.append(paipu_id)
    return paipu_ids


async def batch_download(
    csv_path: str,
    delay: float = 1.0,
    skip_existing: bool = False
):
    """
    Batch download game records
    
    Args:
        csv_path: Path to CSV file
        delay: Delay between requests (seconds)
        skip_existing: Whether to skip existing files
    """
    # Load credentials
    config = load_credentials()
    if not config:
        print("Error: No credentials found. Please run 'python paipu_dl.py --capture' first.")
        return
    
    # Read paipu ID list
    paipu_ids = load_paipu_ids_from_csv(csv_path)
    if not paipu_ids:
        print(f"Error: No paipu IDs found in {csv_path}")
        return
    
    print(f"Found {len(paipu_ids)} records to download")
    print(f"Output directory: {OUTPUT_DIR}")
    print(f"Request delay: {delay} seconds")
    print(f"Skip existing: {skip_existing}")
    print("-" * 50)
    
    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Statistics
    success_count = 0
    skip_count = 0
    fail_count = 0
    failed_ids = []
    
    for i, paipu_id in enumerate(paipu_ids, 1):
        output_file = OUTPUT_DIR / f"{paipu_id}.json"
        
        # Check if already exists
        if skip_existing and output_file.exists():
            print(f"[{i}/{len(paipu_ids)}] Skipped {paipu_id} (already exists)")
            skip_count += 1
            continue
        
        print(f"[{i}/{len(paipu_ids)}] Downloading {paipu_id} ...", end=" ", flush=True)
        
        try:
            await download_record(config, paipu_id)
            
            # Check if successful
            if output_file.exists():
                print("✓ Success")
                success_count += 1
            else:
                print("✗ Failed (file not generated)")
                fail_count += 1
                failed_ids.append(paipu_id)
        except Exception as e:
            print(f"✗ Failed ({e})")
            fail_count += 1
            failed_ids.append(paipu_id)
        
        # Delay to avoid rate limiting
        if i < len(paipu_ids):
            await asyncio.sleep(delay)
    
    # Print statistics
    print("-" * 50)
    print("Download complete!")
    print(f"  Success: {success_count}")
    print(f"  Skipped: {skip_count}")
    print(f"  Failed: {fail_count}")
    
    if failed_ids:
        print(f"\nFailed paipu IDs:")
        for pid in failed_ids:
            print(f"  - {pid}")


def main():
    parser = argparse.ArgumentParser(description="Batch download Majsoul game records")
    parser.add_argument("csv_file", type=str, help="Path to CSV file containing paipu IDs")
    parser.add_argument("--delay", type=float, default=1.0, help="Request delay in seconds (default: 1.0)")
    parser.add_argument("--skip-existing", action="store_true", help="Skip already downloaded records")
    
    args = parser.parse_args()
    
    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        print(f"Error: File not found {csv_path}")
        sys.exit(1)
    
    asyncio.run(batch_download(
        str(csv_path),
        delay=args.delay,
        skip_existing=args.skip_existing
    ))


if __name__ == "__main__":
    main()
