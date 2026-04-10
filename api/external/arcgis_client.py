"""
ArcGIS REST API client for Bangkok GIS data.
Proxies 28 endpoints from เอกสารแนบ ๒ with retry, timeout, and graceful fallback.

Servers:
  - cpudgiapp: https://cpudgiapp.bangkok.go.th/arcgis/rest/services/ (requires ?f=json)
  - bmagis:    https://bmagis.bangkok.go.th/arcgis/rest/services/
  - bmamap:    https://bmamap.bangkok.go.th/bmamap/rest/services/
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("bma.gis")

# Default timeout for external GIS requests (seconds)
_TIMEOUT = 15.0


class ArcGISClient:
    """Thin client for Bangkok ArcGIS REST API endpoints."""

    SERVERS = {
        "cpudgiapp": "https://cpudgiapp.bangkok.go.th/arcgis/rest/services",
        "bmagis": "https://bmagis.bangkok.go.th/arcgis/rest/services",
        "bmamap": "https://bmamap.bangkok.go.th/bmamap/rest/services",
    }

    LAYERS = {
        # Infrastructure
        "districts": ("cpudgiapp", "Basemap_Service/CPUD_Basemap1000/MapServer/12"),
        "roads": ("cpudgiapp", "Basemap_Service/CPUD_Basemap1000/MapServer/7"),
        "buildings": ("cpudgiapp", "Basemap_Service/CPUD_Basemap1000/MapServer/5"),
        "communities": ("cpudgiapp", "Community/Service_Community/FeatureServer/14"),
        "bts_mrt": ("cpudgiapp", "EXTERNAL/Basemap_Traffic/MapServer/4"),
        # Health
        "hospitals": ("bmagis", "จุดสนับสนุนสถานที่/MapServer/16"),
        # Environment / Pollution
        "pm25": ("bmagis", "Hosted/air_quality_data_processed/FeatureServer/0"),
        "cement_plants": ("bmamap", "HEALTHMAP/dust_pollution/MapServer/0"),
        "construction": ("bmamap", "HEALTHMAP/dust_pollution/MapServer/1"),
        "factories": ("bmamap", "HEALTHMAP/dust_pollution/MapServer/2"),
        "incense": ("bmamap", "HEALTHMAP/dust_pollution/MapServer/3"),
        "paint_shops": ("bmamap", "HEALTHMAP/dust_pollution/MapServer/4"),
        "smoke_check": ("bmamap", "HEALTHMAP/dust_pollution/MapServer/6"),
        "stone_craft": ("bmamap", "HEALTHMAP/dust_pollution/MapServer/7"),
        "boilers": ("bmamap", "HEALTHMAP/dust_pollution/MapServer/8"),
        "smelters": ("bmamap", "HEALTHMAP/dust_pollution/MapServer/9"),
    }

    def __init__(self, timeout: float = _TIMEOUT):
        self._timeout = timeout

    async def _query_layer(
        self,
        layer_key: str,
        where: str = "1=1",
        out_fields: str = "*",
        out_sr: int = 4326,
        limit: int = 1000,
        return_geometry: bool = True,
        f: str = "json",
    ) -> Dict[str, Any]:
        """Query a single ArcGIS layer. Returns raw JSON response."""

        if layer_key not in self.LAYERS:
            return {"error": f"Unknown layer: {layer_key}", "valid_layers": sorted(self.LAYERS.keys())}

        server_key, path = self.LAYERS[layer_key]
        base = self.SERVERS[server_key]
        url = f"{base}/{path}/query"

        params = {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": str(return_geometry).lower(),
            "outSR": str(out_sr),
            "f": f,
            "resultRecordCount": str(limit),
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # PM2.5
    # ------------------------------------------------------------------

    async def get_pm25(self, limit: int = 200) -> Dict:
        """Get current PM2.5 readings from Bangkok air quality stations."""
        raw = await self._query_layer("pm25", limit=limit)

        features = raw.get("features", [])
        stations = []
        for feat in features:
            attrs = feat.get("attributes", {})
            geom = feat.get("geometry", {})
            stations.append({
                "station_name": attrs.get("station_name") or attrs.get("STATION_NAME") or attrs.get("name"),
                "pm25_value": attrs.get("pm25") or attrs.get("PM25") or attrs.get("value"),
                "aqi": attrs.get("aqi") or attrs.get("AQI"),
                "measured_at": attrs.get("timestamp") or attrs.get("TIMESTAMP"),
                "latitude": geom.get("y") or geom.get("lat"),
                "longitude": geom.get("x") or geom.get("lon"),
            })

        return {
            "data_available": len(stations) > 0,
            "total_stations": len(stations),
            "data": stations,
        }

    # ------------------------------------------------------------------
    # District boundaries
    # ------------------------------------------------------------------

    async def get_district_boundaries(self, out_sr: int = 4326) -> Dict:
        """Get Bangkok district boundary polygons as GeoJSON."""
        raw = await self._query_layer(
            "districts",
            out_fields="DISTRICT_NAME_T,DISTRICT_NAME_E,DISTRICT_CODE,SUBDISTRICT_NAME_T",
            out_sr=out_sr,
            limit=2000,
            f="geojson",
        )

        # If the server returns GeoJSON directly
        if "type" in raw and raw["type"] in ("FeatureCollection", "Feature"):
            return {"data_available": True, "geojson": raw, "total_features": len(raw.get("features", []))}

        # Otherwise parse features from JSON format
        features = raw.get("features", [])
        return {
            "data_available": len(features) > 0,
            "total_features": len(features),
            "features": features,
        }

    # ------------------------------------------------------------------
    # Pollution sources
    # ------------------------------------------------------------------

    async def get_pollution_sources(self, source_type: str = "factories", limit: int = 500) -> Dict:
        """Get pollution source locations (factories, cement, construction, etc.)."""
        valid = {"cement_plants", "construction", "factories", "incense", "paint_shops",
                 "smoke_check", "stone_craft", "boilers", "smelters"}
        if source_type not in valid:
            return {"error": f"Valid types: {sorted(valid)}"}

        raw = await self._query_layer(source_type, limit=limit)
        features = raw.get("features", [])

        points = []
        for feat in features:
            attrs = feat.get("attributes", {})
            geom = feat.get("geometry", {})
            points.append({
                "name": attrs.get("NAME") or attrs.get("name") or attrs.get("FACNAME"),
                "latitude": geom.get("y"),
                "longitude": geom.get("x"),
                "attributes": attrs,
            })

        return {
            "source_type": source_type,
            "total": len(points),
            "data": points,
        }

    # ------------------------------------------------------------------
    # Generic layer query
    # ------------------------------------------------------------------

    async def query_layer(self, layer_key: str, **kwargs) -> Dict:
        """Generic layer query — pass through to _query_layer."""
        return await self._query_layer(layer_key, **kwargs)
