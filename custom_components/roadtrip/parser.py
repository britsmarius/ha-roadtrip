"""Decoder for Road Trip (iOS, darrensoft) vehicle backups.

A backup is a .zip holding one ``<Vehicle>.roadtrip`` file per vehicle.
Each .roadtrip file is a short text header ("RTVD ... Format:2") followed by
a small binary prelude and a gzip stream containing a protobuf message.

The protobuf schema is not published; field numbers below were worked out
from real backups:

  root.2  vehicle   {1 name, 8 currency, 13 uuid (2 x fixed64)}
  root.3  fill-up   {1 odometer (float), 2 date {1 y, 2 m, 3 d, 4 minute-of-day},
                     3 volume (float), 5 price/unit (float), 6 partial (bool),
                     7 note, 8 fuel type, 9 station, 16 location {1 lon, 2 lat}}
  root.5  trip, root.6 tires, root.7 misc (ignored)

This module is pure Python (no Home Assistant imports) so it can be
tested on its own.
"""

from __future__ import annotations

import io
import re
import statistics
import struct
import zipfile
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

MAGIC = b"RTVD"


class RoadTripParseError(Exception):
    """Raised when data is not a readable Road Trip file."""


# --------------------------------------------------------------------------
# Minimal protobuf wire-format reader
# --------------------------------------------------------------------------


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, i


def _fields(buf: bytes) -> list[tuple[int, int, object]]:
    """Return (field_number, wire_type, raw_value) for one message level."""
    out = []
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        num, wtype = key >> 3, key & 7
        if num == 0:
            raise ValueError("field 0")
        if wtype == 0:
            val, i = _varint(buf, i)
        elif wtype == 1:
            val = buf[i : i + 8]
            i += 8
        elif wtype == 5:
            val = buf[i : i + 4]
            i += 4
        elif wtype == 2:
            ln, i = _varint(buf, i)
            val = buf[i : i + ln]
            i += ln
        else:
            raise ValueError(f"unsupported wire type {wtype}")
        if i > len(buf):
            raise ValueError("truncated field")
        out.append((num, wtype, val))
    return out


def _first(fields, num, default=None):
    for n, _w, v in fields:
        if n == num:
            return v
    return default


def _f32(v) -> float | None:
    return struct.unpack("<f", v)[0] if isinstance(v, bytes) and len(v) == 4 else None


def _f64(v) -> float | None:
    return struct.unpack("<d", v)[0] if isinstance(v, bytes) and len(v) == 8 else None


def _str(v) -> str | None:
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class FillUp:
    when: datetime  # naive local time as entered in the app
    odometer: float
    volume: float
    price: float
    partial: bool = False
    station: str | None = None
    note: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    @property
    def cost(self) -> float:
        return self.volume * self.price


@dataclass
class Vehicle:
    vehicle_id: str
    name: str
    currency: str
    fillups: list[FillUp] = field(default_factory=list)


def parse_vehicle_file(data: bytes, fallback_name: str = "Vehicle") -> Vehicle:
    """Decode one .roadtrip file."""
    if not data.startswith(MAGIC):
        raise RoadTripParseError("not a Road Trip vehicle file")
    gz = data.find(b"\x1f\x8b\x08")
    if gz < 0:
        raise RoadTripParseError("no compressed payload found")
    try:
        payload = zlib.decompressobj(31).decompress(data[gz:])
        root = _fields(payload)
    except (zlib.error, ValueError) as err:
        raise RoadTripParseError(f"corrupt payload: {err}") from err

    vinfo_raw = _first(root, 2, b"")
    vinfo = _fields(vinfo_raw) if vinfo_raw else []
    name = _str(_first(vinfo, 1)) or fallback_name
    currency = _str(_first(vinfo, 8)) or ""
    uuid_raw = _first(vinfo, 13)
    if isinstance(uuid_raw, bytes) and uuid_raw:
        vid = uuid_raw.hex()
    else:
        vid = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")

    vehicle = Vehicle(vehicle_id=vid, name=name, currency=currency)
    for num, wtype, val in root:
        if num != 3 or wtype != 2:
            continue
        try:
            f = _fields(val)
            date = _fields(_first(f, 2, b""))
            year, month, day = _first(date, 1), _first(date, 2), _first(date, 3)
            minute = _first(date, 4, 720)
            when = datetime(year, month, day, minute // 60 % 24, minute % 60)
            odo = _f32(_first(f, 1))
            vol = _f32(_first(f, 3))
            price = _f32(_first(f, 5)) or 0.0
            if odo is None or vol is None:
                continue
            lat = lon = None
            loc = _first(f, 16)
            if isinstance(loc, bytes) and loc:
                lf = _fields(loc)
                lon, lat = _f64(_first(lf, 1)), _f64(_first(lf, 2))
            vehicle.fillups.append(
                FillUp(
                    when=when,
                    odometer=round(odo, 1),
                    volume=round(vol, 3),
                    price=round(price, 3),
                    partial=bool(_first(f, 6, 0)),
                    station=_str(_first(f, 9)),
                    note=_str(_first(f, 7)),
                    latitude=lat,
                    longitude=lon,
                )
            )
        except (TypeError, ValueError):
            # Skip a malformed record rather than failing the whole import.
            continue
    # Odometer order is the ground truth; same-day entries can have odd times.
    vehicle.fillups.sort(key=lambda z: (z.odometer, z.when))
    return vehicle


def extract_vehicle_files(data: bytes, filename: str | None = None) -> list[tuple[str, bytes]]:
    """Split an upload into (vehicle file stem, .roadtrip bytes) pairs.

    Accepts a backup .zip (any folder depth) or a single .roadtrip file.
    """
    if data[:2] == b"PK":
        files = []
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    base = info.filename.rsplit("/", 1)[-1]
                    if info.is_dir() or base.startswith(".") or not base.endswith(".roadtrip"):
                        continue
                    files.append((base[: -len(".roadtrip")], zf.read(info)))
        except zipfile.BadZipFile as err:
            raise RoadTripParseError(f"bad zip: {err}") from err
        if not files:
            raise RoadTripParseError("zip contains no .roadtrip files")
        return files
    if data.startswith(MAGIC):
        stem = (filename or "Vehicle").replace("\\", "/").rsplit("/", 1)[-1]
        stem = stem[: -len(".roadtrip")] if stem.endswith(".roadtrip") else stem
        return [(stem, data)]
    raise RoadTripParseError("unrecognised file (expected a Road Trip backup .zip or .roadtrip)")


def parse_upload(data: bytes, filename: str | None = None) -> list[Vehicle]:
    """Decode a backup .zip or a single .roadtrip file."""
    return [parse_vehicle_file(raw, stem) for stem, raw in extract_vehicle_files(data, filename)]


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------

OUTLIER_FACTOR = 1.8  # tanks above 1.8x median are almost always a missed fill-up
ROLLING_TANKS = 8


def _ms(dt: datetime, tz: tzinfo | None) -> int:
    if tz is not None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp() * 1000)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "vehicle"


def vehicle_stats(v: Vehicle, now: datetime, tz: tzinfo | None = None) -> dict:
    """Compute consumption, cost and chart series for one vehicle.

    ``now`` is naive local time (same convention as the fill-up dates).
    Consumption uses the full-tank-to-full-tank method: litres from any
    partial fills in between are added to the next full tank.
    """
    f = v.fillups
    out: dict = {
        "id": v.vehicle_id,
        "slug": _slug(v.name),
        "name": v.name,
        "currency": v.currency,
        "fillups": len(f),
    }
    if not f:
        return out

    # Per-tank consumption
    tanks = []  # (fillup, l_per_100, segment_km)
    last_full = None
    pending = 0.0
    for z in f:
        if z.partial:
            pending += z.volume
            continue
        if last_full is not None:
            dist = z.odometer - last_full.odometer
            if dist > 50:
                tanks.append((z, 100 * (pending + z.volume) / dist, dist))
        last_full = z
        pending = 0.0

    good = tanks
    if tanks:
        med = statistics.median(t[1] for t in tanks)
        good = [t for t in tanks if t[1] < OUTLIER_FACTOR * med]

    first, last = f[0], f[-1]
    km = last.odometer - first.odometer
    litres_after_first = sum(z.volume for z in f[1:])
    year_ago = now - timedelta(days=365)
    in_year = [z for z in f if z.when >= year_ago]
    before = [z for z in f if z.when < year_ago]
    base = before[-1] if before else first
    km_12 = last.odometer - base.odometer if in_year else 0.0
    spend_12 = sum(z.cost for z in in_year)
    cost_12_for_km = sum(z.cost for z in in_year if z is not first)
    recent = good[-5:]

    def r(x, n=2):
        return None if x is None else round(x, n)

    out.update(
        first_date=first.when.isoformat(),
        distance_logged=r(km, 1),
        total_volume=r(sum(z.volume for z in f), 1),
        total_spend=r(sum(z.cost for z in f)),
        economy_lifetime=r(100 * litres_after_first / km if km > 0 else None),
        economy_recent=r(sum(t[1] * t[2] for t in recent) / sum(t[2] for t in recent) if recent else None),
        cost_per_km_lifetime=r(sum(z.cost for z in f[1:]) / km if km > 0 else None, 3),
        distance_12m=r(km_12, 1),
        spend_12m=r(spend_12),
        cost_per_km_12m=r(cost_12_for_km / km_12 if km_12 > 0 else None, 3),
        best_tank=r(min((t[1] for t in good), default=None)),
        worst_tank=r(max((t[1] for t in good), default=None)),
    )
    l100_by_fill = {id(t[0]): round(t[1], 2) for t in tanks}
    out["last_fill"] = {
        "date": last.when.isoformat(),
        "timestamp": _ms(last.when, tz),
        "odometer": last.odometer,
        "volume": round(last.volume, 2),
        "price": round(last.price, 3),
        "cost": round(last.cost, 2),
        "partial": last.partial,
        "station": last.station,
        "economy": l100_by_fill.get(id(last)),
    }
    out["recent"] = [
        {
            "date": z.when.date().isoformat(),
            "odometer": z.odometer,
            "volume": round(z.volume, 2),
            "price": round(z.price, 2),
            "cost": round(z.cost, 2),
            "partial": z.partial,
            "economy": l100_by_fill.get(id(z)),
        }
        for z in reversed(f[-10:])
    ]

    yearly: dict[int, dict] = defaultdict(lambda: {"spend": 0.0, "volume": 0.0, "distance": 0.0})
    monthly: dict[tuple[int, int], float] = defaultdict(float)
    for i, z in enumerate(f):
        y = yearly[z.when.year]
        y["spend"] += z.cost
        monthly[(z.when.year, z.when.month)] += z.cost
        if i:
            y["distance"] += z.odometer - f[i - 1].odometer
            y["volume"] += z.volume
    out["yearly"] = [
        {
            "year": yr,
            "distance": round(d["distance"]),
            "volume": round(d["volume"], 1),
            "spend": round(d["spend"], 2),
            "economy": round(100 * d["volume"] / d["distance"], 2) if d["distance"] > 200 else None,
            "cost_per_km": round(d["spend"] / d["distance"], 3) if d["distance"] > 200 else None,
        }
        for yr, d in sorted(yearly.items(), reverse=True)
    ]

    rolling = []
    for i in range(len(good)):
        window = good[max(0, i - ROLLING_TANKS + 1) : i + 1]
        dist = sum(t[2] for t in window)
        rolling.append([_ms(good[i][0].when, tz), round(sum(t[1] * t[2] for t in window) / dist, 2)])

    out["series"] = {
        "economy": [[_ms(t[0].when, tz), round(t[1], 2)] for t in good],
        "economy_rolling": rolling,
        "price": [[_ms(z.when, tz), round(z.price, 2)] for z in f],
        "odometer": [[_ms(z.when, tz), z.odometer] for z in f],
        "spend_monthly": [
            [_ms(datetime(y, m, 15, 12), tz), round(c, 2)] for (y, m), c in sorted(monthly.items())
        ],
        "spend_yearly": [
            [_ms(datetime(yr, 7, 1, 12), tz), round(d["spend"], 2)] for yr, d in sorted(yearly.items())
        ],
    }
    return out


def build_dataset(vehicles: list[Vehicle], now: datetime, tz: tzinfo | None = None) -> dict:
    """Full JSON-able dataset for sensors and the dashboard API."""
    stats = [vehicle_stats(v, now, tz) for v in vehicles]
    # Make slugs unique if two vehicles share a name.
    seen: dict[str, int] = {}
    for s in stats:
        n = seen.get(s["slug"], 0)
        seen[s["slug"]] = n + 1
        if n:
            s["slug"] = f"{s['slug']}_{n + 1}"
    stats.sort(key=lambda s: s.get("last_fill", {}).get("timestamp", 0), reverse=True)
    return {
        "generated": now.isoformat(),
        "vehicles": stats,
        "fleet": {
            "total_spend": round(sum(s.get("total_spend") or 0 for s in stats), 2),
            "spend_12m": round(sum(s.get("spend_12m") or 0 for s in stats), 2),
            "fillups": sum(s["fillups"] for s in stats),
        },
    }
