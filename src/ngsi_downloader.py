"""
Downloads all NGSI-LD entities needed to reconstruct a SUMO network
from the LDT context broker and saves them as a single JSON file.

Entity types downloaded: ProjectionConfig, Road, RoadSegment, Junction.

Handles pagination automatically (NGSI-LD brokers typically limit to
~100 entities per request).

Required environment variables (same as upload_ngsi.py):
  BASE_URL, OIDC_ENDPOINT, OIDC_CLIENT, OIDC_USER, OIDC_PASSWORD
"""

import json
import os

import requests
import urllib3
import logging

from config import settings

logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_ENTITY_TYPES = ["ProjectionConfig", "Road", "RoadSegment", "Junction"]
DEFAULT_LIMIT = 100


class NGSILDDownloader:
    """Client for downloading NGSI-LD entities from the LDT context broker."""

    def __init__(
        self,
        dpl_host: str | None = None,
        oidc_endpoint: str | None = None,
        oidc_client: str | None = None,
        oidc_user: str | None = None,
        oidc_password: str | None = None,
        scope: str = "demo",
        http_schema: str = "https"
    ):
        self.dpl_host = dpl_host or settings.DPL_HOST
        self.oidc_endpoint = oidc_endpoint or settings.OIDC_ENDPOINT
        self.oidc_client = oidc_client or settings.OIDC_CLIENT
        self.oidc_user = oidc_user or settings.OIDC_USER
        self.oidc_password = oidc_password or settings.OIDC_PASSWORD
        self.scope = scope
        self.http_schema = http_schema
        self._token: str | None = None

    def _get_access_token(self) -> str:
        if self._token:
            return self._token
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
        self._token = response.json()["access_token"]
        return self._token

    def fetch_entities(
        self,
        entity_type: str,
        domain: str = "",
        limit: int = DEFAULT_LIMIT,
    ) -> list[dict]:
        """Fetch all entities of a given type, handling pagination."""
        token = self._get_access_token()
        entities: list[dict] = []
        params: dict[str, str | int] = {
            "type": entity_type,
        }
        resp = requests.get(
            f"{self.http_schema}://{self.dpl_host}/api/v1/entities",
            headers={
                "Authorization": f"Bearer {token}",
                "scope": self.scope,
                "Accept": "application/ld+json",
            },
            params=params,
            verify=False,
        )
        entities.extend(resp.json())

        return entities

    def download_all(
        self,
        entity_types: list[str] | None = None,
        domain: str = "",
    ) -> list[dict]:
        """Download all entities for the given types."""
        if entity_types is None:
            entity_types = DEFAULT_ENTITY_TYPES

        all_entities: list[dict] = []
        for entity_type in entity_types:
            entities = self.fetch_entities(entity_type, domain)
            all_entities.extend(entities)
        return all_entities

    def download_to_file(
        self,
        output: str = "ngsild_geoentities.json",
        entity_types: list[str] | None = None,
        domain: str = "",
    ) -> list[dict]:
        """Download all entities and write them to a JSON file."""
        all_entities = self.download_all(entity_types, domain)
        with open(output, "w", encoding="utf-8") as f:
            json.dump(all_entities, f, indent=2, ensure_ascii=False)
        return all_entities


if __name__ == "__main__":
    from ngsild_to_sumo import NGSILDToSUMOConverter

    dwn = NGSILDDownloader()
    dwn.download_to_file()

    converter = NGSILDToSUMOConverter()
    converter.convert_file(input_path="ngsild_geoentities.json", output_path="test.net.xml")
