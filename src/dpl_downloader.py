"""
DPL (Data Platform) Downloader for SUMO simulation data.

Downloads detectors and flow data from the DPL Trino database.
"""

import os
import logging
import requests
from typing import List, Dict, Any

from trino.dbapi import connect
from trino.auth import JWTAuthentication

from config import settings


logger = logging.getLogger(__name__)


class DPLDownloader:
    """Client for downloading SUMO simulation data from the DPL."""

    def __init__(
        self,
        dpl_host: str = settings.BASE_DQ_URL,
        oidc_endpoint: str = settings.OIDC_ENDPOINT,
        oidc_client: str = settings.OIDC_CLIENT,
        oidc_user: str = settings.OIDC_USER,
        oidc_password: str = settings.OIDC_PASSWORD,
        port: int = settings.DPL_PORT,
        http_scheme: str = "HTTPS",
        verify: bool = False,
    ):
        """
        Initialize DPL downloader.

        Args:
            host: DPL host address
            port: DPL port
            http_scheme: HTTP or HTTPS
            verify: Whether to verify SSL certificates
            user: Username for authentication
            access_token: JWT access token (defaults to ACCESS_TOKEN env var)
        """
        self.host = dpl_host
        self.port = port
        self.http_scheme = http_scheme
        self.verify = verify
        self.oidc_endpoint = oidc_endpoint
        self.oidc_client = oidc_client
        self.oidc_user = oidc_user
        self.oidc_password = oidc_password
        self.access_token = self._get_token()

        self._conn = None

    def _get_token(self):
        response = requests.post(
            f"{self.oidc_endpoint}/protocol/openid-connect/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "password",
                "client_id": self.oidc_client,
                "username": self.oidc_user,
                "password": self.oidc_password,
                "scope": "openid profile email",
            },
            verify=False,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def _get_connection(self):
        """Get or create a connection to DPL."""
        if self._conn is None:
            self._conn = connect(
                host=self.host,
                port=self.port,
                http_scheme=self.http_scheme,
                verify=self.verify,
                user=self.oidc_user,
                auth=JWTAuthentication(self.access_token),
            )
        return self._conn

    def _execute_query(self, query: str) -> List[tuple]:
        """Execute a query and return results."""
        conn = self._get_connection()
        cur = conn.cursor()
        logger.info(f"Executing DPL query: {query}")
        cur.execute(query)
        rows = cur.fetchall()
        logger.info(f"Query returned {len(rows)} rows")
        return rows

    def download_detectors(
        self,
        catalog: str = "object",
        schema: str = "ldt_data",
        table: str = "urban_flow_detectors",
        domain: str | None = None,
        lat_min: float | None = None,
        lat_max: float | None = None,
        lon_min: float | None = None,
        lon_max: float | None = None,
    ) -> List[Dict[str, Any]]:
        """
        Download detector data from DPL.

        Args:
            catalog: Trino catalog name
            schema: Trino schema name
            table: Table name for detectors
            domain: Optional domain filter
            lat_min: Minimum latitude filter
            lat_max: Maximum latitude filter
            lon_min: Minimum longitude filter
            lon_max: Maximum longitude filter

        Returns:
            List of detector dictionaries with keys: detector_id, lon, lat
        """
        # Build query with filters
        query = f"SELECT detector_id, lon, lat, device_id FROM {catalog}.{schema}.{table}"
        filters = []

        if domain:
            filters.append(f"domain = '{domain}'")
        if lat_min is not None:
            filters.append(f"CAST(lat AS REAL) >= {lat_min}")
        if lat_max is not None:
            filters.append(f"CAST(lat AS REAL) < {lat_max}")
        if lon_min is not None:
            filters.append(f"CAST(lon AS REAL) >= {lon_min}")
        if lon_max is not None:
            filters.append(f"CAST(lon AS REAL) < {lon_max}")

        if filters:
            query += " WHERE " + " AND ".join(filters)

        rows = self._execute_query(query)

        # Convert to list of dicts
        detectors = []
        for row in rows:
            detectors.append({
                "detector_id": str(row[0]),
                "lon": float(row[1]),
                "lat": float(row[2]),
            })

        logger.info(f"Downloaded {len(detectors)} detectors from DPL")
        return detectors

    def download_flow_data(
        self,
        catalog: str = "object",
        schema: str = "ldt_data",
        table: str = "urban_flow_data",
        domain: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> List[List[Any]]:
        """
        Download flow data from DPL.

        Args:
            catalog: Trino catalog name
            schema: Trino schema name
            table: Table name for flow data
            domain: Optional domain filter
            start_time: Minimum time filter (minutes)
            end_time: Maximum time filter (minutes)

        Returns:
            List of lists in format: [[detector_id, time, passenger_count], ...]
        """
        # Build query with filters
        query = f"SELECT detector_id, time, flow FROM {catalog}.{schema}.{table}"
        filters = []

        if domain:
            filters.append(f"domain = '{domain}'")
        if start_time is not None:
            filters.append(f"time >= {start_time}")
        if end_time is not None:
            filters.append(f"time <= {end_time}")

        if filters:
            query += " WHERE " + " AND ".join(filters)

        query += " ORDER BY detector_id, time"

        rows = self._execute_query(query)

        # Convert to list of lists
        flow_data = [[int(row[0]), int(row[1]), int(row[2])] for row in rows]

        logger.info(f"Downloaded {len(flow_data)} flow data records from DPL")
        return flow_data

    def close(self):
        """Close the connection to DPL."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
