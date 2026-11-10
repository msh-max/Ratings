"""CLI: python -m ranking build|qa --month YYYY-MM"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .build import build, write_outputs
from .geocode import Geocoder
from .load import latest_month
from .qa import report


def _month(args: argparse.Namespace) -> str:
    month = args.month or latest_month(Path(args.root))
    if not month:
        sys.exit("error: no --month given and no extracts under data/sources/")
    return month


def cmd_build(args: argparse.Namespace) -> int:
    root = Path(args.root)
    month = _month(args)
    # --check never touches the network or disk: cache-only geocoding.
    online = not (args.no_geocode or args.check)
    geocoder = Geocoder(root / "data" / "geocode" / "cache.json", online=online)
    res = build(root, month, geocoder=geocoder)
    for w in res.warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in res.errors:
        print(f"error: {e}", file=sys.stderr)
    if res.errors or res.output is None:
        print(f"build {month}: FAILED ({len(res.errors)} errors)", file=sys.stderr)
        return 1
    out = res.output
    geocoded = sum(1 for p in out["places"] if p["lat"] is not None)
    summary = (f"build {month}: {out['n_places']} places from {out['n_sources']} sources; "
               f"geocoded {geocoded}; {res.geocode_requests} Nominatim requests")
    if args.check:
        print(summary + " — check OK (nothing written)")
        return 0
    geocoder.save()
    for path in write_outputs(root, out, month):
        print(f"wrote {path}")
    print(summary)
    return 0


def cmd_qa(args: argparse.Namespace) -> int:
    root = Path(args.root)
    month = _month(args)
    res = build(root, month, geocoder=Geocoder(root / "data" / "geocode" / "cache.json", online=False))
    return report(res.output, res.errors, res.warnings, sys.stdout, top=args.top)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m ranking",
                                description="Consensus ranking of Riyadh restaurants from published sources.")
    p.add_argument("--root", default=".", help="repository root (default: current directory)")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build data/rankings/restaurants/<month>.json, latest.json and docs/data/restaurants.json")
    b.add_argument("--month", help="YYYY-MM (default: latest month under data/sources)")
    b.add_argument("--no-geocode", action="store_true", help="do not call Nominatim; use cached coordinates only")
    b.add_argument("--check", action="store_true", help="validate extracts and output; write nothing, no network")
    b.set_defaults(func=cmd_build)

    q = sub.add_parser("qa", help="print top places, duplicate suspects, gaps, non-Riyadh suspects")
    q.add_argument("--month", help="YYYY-MM (default: latest month under data/sources)")
    q.add_argument("--top", type=int, default=30)
    q.set_defaults(func=cmd_qa)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:          # e.g. `python -m ranking qa | head`
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(1)
