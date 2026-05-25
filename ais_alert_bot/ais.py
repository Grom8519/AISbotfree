from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import ssl
from typing import Any, Protocol

import httpx
import websockets

from .config import Settings


class AISProviderError(RuntimeError):
    pass


class VesselNotFound(AISProviderError):
    pass


@dataclass(frozen=True)
class VesselPosition:
    query: str
    mmsi: str | None
    name: str | None
    lat: float
    lon: float
    timestamp: str | None
    source: str | None = None
    speed_knots: float | None = None
    course: float | None = None


class AISProvider(Protocol):
    async def latest_position_by_mmsi(self, mmsi: str) -> VesselPosition:
        ...

    async def latest_position_by_imo(self, imo: str) -> VesselPosition:
        ...

    async def latest_position_by_name(self, name: str) -> VesselPosition:
        ...


def _first_payload_item(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list) and payload:
        item = payload[0]
        if isinstance(item, dict):
            return item
    if isinstance(payload, dict):
        data = payload.get("DATA")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        if "AIS" in payload and isinstance(payload["AIS"], dict):
            return payload
    raise VesselNotFound("AIS provider returned no vessel positions")


def _float_field(data: dict[str, Any], *names: str) -> float:
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return float(value)
    raise AISProviderError(f"Missing required coordinate field: {', '.join(names)}")


class MarineTrafficProvider:
    base_url = "https://services.marinetraffic.com/api"

    def __init__(self, api_key: str, timespan_minutes: int = 1440) -> None:
        if not api_key:
            raise AISProviderError("MARINETRAFFIC_API_KEY is not configured")
        self.api_key = api_key
        self.timespan_minutes = timespan_minutes
        self.client = httpx.AsyncClient(timeout=20)

    async def latest_position_by_mmsi(self, mmsi: str) -> VesselPosition:
        return await self._latest_position({"mmsi": mmsi}, query=mmsi)

    async def latest_position_by_imo(self, imo: str) -> VesselPosition:
        return await self._latest_position({"imo": imo}, query=imo)

    async def _latest_position(self, vessel_param: dict[str, str], query: str) -> VesselPosition:
        url = f"{self.base_url}/exportvessel/{self.api_key}"
        params = {
            "v": 6,
            "timespan": self.timespan_minutes,
            "protocol": "jsono",
            **vessel_param,
        }
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        item = _first_payload_item(response.json())

        return VesselPosition(
            query=query,
            mmsi=str(item.get("MMSI")) if item.get("MMSI") else None,
            name=item.get("SHIPNAME"),
            lat=_float_field(item, "LAT"),
            lon=_float_field(item, "LON"),
            timestamp=item.get("TIMESTAMP"),
            source=item.get("DSRC"),
            speed_knots=_safe_float(item.get("SPEED")),
            course=_safe_float(item.get("COURSE")),
        )

    async def latest_position_by_name(self, name: str) -> VesselPosition:
        raise AISProviderError(
            "MarineTraffic name lookup is contract-dependent. Resolve the vessel name to MMSI and use /watch mmsi."
        )


class VesselFinderProvider:
    base_url = "https://api.vesselfinder.com"

    def __init__(self, api_key: str, include_satellite: bool = False, interval_minutes: int = 1440) -> None:
        if not api_key:
            raise AISProviderError("VESSELFINDER_API_KEY is not configured")
        self.api_key = api_key
        self.include_satellite = include_satellite
        self.interval_minutes = interval_minutes
        self.client = httpx.AsyncClient(timeout=20)

    async def latest_position_by_mmsi(self, mmsi: str) -> VesselPosition:
        return await self._latest_position({"mmsi": mmsi}, query=mmsi)

    async def latest_position_by_imo(self, imo: str) -> VesselPosition:
        return await self._latest_position({"imo": imo}, query=imo)

    async def _latest_position(self, vessel_param: dict[str, str], query: str) -> VesselPosition:
        params: dict[str, Any] = {
            "userkey": self.api_key,
            "format": "json",
            "interval": self.interval_minutes,
            "errormode": 409,
            **vessel_param,
        }
        if self.include_satellite:
            params["sat"] = 1

        response = await self.client.get(f"{self.base_url}/vessels", params=params)
        response.raise_for_status()
        item = _first_payload_item(response.json())
        ais = item.get("AIS", item)
        return _vesselfinder_position(ais, query=query)

    async def latest_position_by_name(self, name: str) -> VesselPosition:
        params: dict[str, Any] = {
            "userkey": self.api_key,
            "format": "json",
            "interval": self.interval_minutes,
            "errormode": 409,
        }
        response = await self.client.get(f"{self.base_url}/vesselslist", params=params)
        response.raise_for_status()
        normalized = name.casefold().strip()
        for item in response.json():
            ais = item.get("AIS", item)
            vessel_name = str(ais.get("NAME") or "").casefold().strip()
            if vessel_name == normalized:
                return _vesselfinder_position(ais, query=name)
        raise VesselNotFound(f"Vessel name not found in VesselFinder predefined fleet: {name}")


class AISStreamProvider:
    stream_url = "wss://stream.aisstream.io/v0/stream"
    position_message_types = [
        "PositionReport",
        "StandardClassBPositionReport",
        "ExtendedClassBPositionReport",
    ]

    def __init__(self, api_key: str, wait_seconds: int = 90) -> None:
        if not api_key:
            raise AISProviderError("AISSTREAM_API_KEY is not configured")
        self.api_key = api_key
        self.wait_seconds = wait_seconds
        self.ssl_context = ssl._create_unverified_context()

    async def latest_position_by_mmsi(self, mmsi: str) -> VesselPosition:
        subscribe_message = {
            "APIKey": self.api_key,
            "BoundingBoxes": [[[-90, -180], [90, 180]]],
            "FiltersShipMMSI": [mmsi],
            "FilterMessageTypes": self.position_message_types,
        }
        async with websockets.connect(self.stream_url, ssl=self.ssl_context) as websocket:
            await websocket.send(json.dumps(subscribe_message))
            return await asyncio.wait_for(self._read_position(websocket, query=mmsi), timeout=self.wait_seconds)

    async def latest_position_by_imo(self, imo: str) -> VesselPosition:
        mmsi = await self._resolve_imo_to_mmsi(imo)
        return await self.latest_position_by_mmsi(mmsi)

    async def latest_position_by_name(self, name: str) -> VesselPosition:
        raise AISProviderError("AISStream supports reliable free filtering by MMSI. Use MMSI for tracking.")

    async def _resolve_imo_to_mmsi(self, imo: str) -> str:
        subscribe_message = {
            "APIKey": self.api_key,
            "BoundingBoxes": [[[-90, -180], [90, 180]]],
            "FilterMessageTypes": ["ShipStaticData"],
        }
        async with websockets.connect(self.stream_url, ssl=self.ssl_context) as websocket:
            await websocket.send(json.dumps(subscribe_message))
            return await asyncio.wait_for(self._read_static_mmsi(websocket, imo), timeout=self.wait_seconds)

    async def _read_position(self, websocket: Any, query: str) -> VesselPosition:
        async for raw_message in websocket:
            payload = json.loads(raw_message)
            message_type = payload.get("MessageType")
            if message_type not in self.position_message_types:
                continue

            body = _aisstream_message_body(payload, message_type)
            lat = _float_field(body, "Latitude")
            lon = _float_field(body, "Longitude")
            metadata = payload.get("MetaData") if isinstance(payload.get("MetaData"), dict) else {}
            return VesselPosition(
                query=query,
                mmsi=str(body.get("UserID") or metadata.get("MMSI") or query),
                name=_clean_ais_name(metadata.get("ShipName")),
                lat=lat,
                lon=lon,
                timestamp=metadata.get("time_utc") or metadata.get("Time_UTC") or str(body.get("Timestamp") or ""),
                source="aisstream",
                speed_knots=_safe_float(body.get("Sog")),
                course=_safe_float(body.get("Cog")),
            )

        raise VesselNotFound("AISStream connection closed before a position was received")

    async def _read_static_mmsi(self, websocket: Any, imo: str) -> str:
        async for raw_message in websocket:
            payload = json.loads(raw_message)
            if payload.get("MessageType") != "ShipStaticData":
                continue
            body = _aisstream_message_body(payload, "ShipStaticData")
            if str(body.get("ImoNumber") or "") == imo:
                mmsi = body.get("UserID")
                if mmsi:
                    return str(mmsi)
        raise VesselNotFound(f"IMO {imo} was not seen in AISStream static data")


def _vesselfinder_position(ais: dict[str, Any], query: str) -> VesselPosition:
    return VesselPosition(
        query=query,
        mmsi=str(ais.get("MMSI")) if ais.get("MMSI") else None,
        name=ais.get("NAME"),
        lat=_float_field(ais, "LATITUDE"),
        lon=_float_field(ais, "LONGITUDE"),
        timestamp=ais.get("TIMESTAMP"),
        source=ais.get("SRC"),
        speed_knots=_safe_float(ais.get("SPEED")),
        course=_safe_float(ais.get("COURSE")),
    )


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _aisstream_message_body(payload: dict[str, Any], message_type: str) -> dict[str, Any]:
    message = payload.get("Message")
    if isinstance(message, dict):
        nested = message.get(message_type)
        if isinstance(nested, dict):
            return nested
        return message
    raise AISProviderError(f"AISStream payload does not contain {message_type}")


def _clean_ais_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.replace("@", "").strip()
    return cleaned or None


def build_provider(settings: Settings) -> AISProvider:
    if settings.ais_provider == "aisstream":
        return AISStreamProvider(
            settings.aisstream_api_key,
            settings.aisstream_wait_seconds,
        )
    if settings.ais_provider == "marinetraffic":
        return MarineTrafficProvider(
            settings.marinetraffic_api_key,
            settings.marinetraffic_timespan_minutes,
        )
    if settings.ais_provider == "vesselfinder":
        return VesselFinderProvider(
            settings.vesselfinder_api_key,
            settings.vesselfinder_include_satellite,
            settings.vesselfinder_interval_minutes,
        )
    raise AISProviderError(f"Unsupported AIS_PROVIDER: {settings.ais_provider}")
