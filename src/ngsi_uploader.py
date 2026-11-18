import logging

import requests
import urllib3

from config import settings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class NGSIUploader:
    """
    Uploads NGSI-LD entities to a broker using OIDC password-flow authentication.

    Configuration is read from `settings` (src/config.py):
      - OIDC_ENDPOINT  : base URL of the OIDC realm (without /protocol/...)
      - OIDC_CLIENT    : client_id
      - OIDC_USER      : username
      - OIDC_PASSWORD  : password
      - NGSI_BASE_URL  : base URL of the NGSI-LD broker
    """

    def __init__(self):
        self._token: str | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        response = requests.post(
            f"{settings.OIDC_ENDPOINT}/protocol/openid-connect/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "password",
                "client_id": settings.OIDC_CLIENT,
                "username": settings.OIDC_USER,
                "password": settings.OIDC_PASSWORD,
                "scope": "openid profile email",
            },
        )
        response.raise_for_status()
        self._token = response.json()["access_token"]
        return self._token

    def _token_or_refresh(self) -> str:
        if self._token is None:
            return self._get_access_token()
        return self._token

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload_entity(self, entity: dict) -> requests.Response:
        """
        Upload a single NGSI-LD entity. Automatically refreshes the token on 401.
        409 (already exists) is silently ignored.
        """
        response = requests.post(
            f"{settings.NGSI_BASE_URL}/api/v1/entities",
            headers={
                "Authorization": f"Bearer {self._token_or_refresh()}",
                "scope": settings.DPL_SCOPE,
                "Content-Type": "application/ld+json",
            },
            json=entity,
            verify=False,
        )
        if response.status_code == 401:
            self._get_access_token()
            response = requests.post(
                f"{settings.NGSI_BASE_URL}/api/v1/entities",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "scope": "demo",
                    "Content-Type": "application/ld+json",
                },
                json=entity,
                verify=False,
            )
        if response.status_code != 409:
            response.raise_for_status()
        return response

    def upload_entities(self, entities: list[dict]) -> list[dict]:
        """
        Upload a list of NGSI-LD entities.

        Returns the list of entities that failed to upload (excluding 409 conflicts).
        """
        self._token_or_refresh()
        failed: list[dict] = []
        for entity in entities:
            try:
                self.upload_entity(entity)
            except Exception as exc:
                entity_id = entity.get("id", "<unknown>")
                logger.error(f"Failed to upload entity {entity_id}: {exc}")
                failed.append(entity)
        if failed:
            logger.warning(f"{len(failed)}/{len(entities)} entities failed to upload")
        else:
            logger.info(f"Uploaded {len(entities)} entities successfully")
        return failed

    def _batch_upsert_chunk(self, chunk: list[dict]) -> requests.Response:
        """POST a single chunk to the entityOperations/upsert endpoint."""
        response = requests.post(
            f"{settings.NGSI_BASE_URL}/cb/ngsi-ld/v1/entityOperations/upsert",
            headers={
                "Authorization": f"Bearer {self._token_or_refresh()}",
                "NGSILD-Tenant": settings.DPL_SCOPE,
                "Content-Type": "application/ld+json",
            },
            json=chunk,
            verify=False,
        )
        if response.status_code == 401:
            self._get_access_token()
            response = requests.post(
                f"{settings.NGSI_BASE_URL}/cb/ngsi-ld/v1/entityOperations/upsert",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "NGSILD-Tenant": settings.DPL_SCOPE,
                    "Content-Type": "application/ld+json",
                },
                json=chunk,
                verify=False,
            )
        response.raise_for_status()
        return response

    def batch_upsert(self, entities: list[dict], batch_size: int = 1000) -> list[dict]:
        """
        Batch upsert NGSI-LD entities via /cb/ngsi-ld/v1/entityOperations/upsert.

        When the total number of entities exceeds `batch_size` (default 1000) the
        list is split into pages of at most `batch_size` entities and each page is
        sent in a separate request.

        Returns the list of entities whose batch failed to upload.
        """
        self._token_or_refresh()
        failed: list[dict] = []
        total = len(entities)
        batches = [entities[i : i + batch_size] for i in range(0, total, batch_size)]
        for batch_num, chunk in enumerate(batches, start=1):
            try:
                self._batch_upsert_chunk(chunk)
                logger.info(
                    f"Batch upsert {batch_num}/{len(batches)}: "
                    f"{len(chunk)} entities uploaded successfully"
                )
            except Exception as exc:
                logger.error(
                    f"Batch upsert {batch_num}/{len(batches)}: "
                    f"{len(chunk)} entities failed: {exc}"
                )
                failed.extend(chunk)
        if failed:
            logger.warning(f"{len(failed)}/{total} entities failed to batch upsert")
        else:
            logger.info(f"Batch upserted {total} entities successfully")
        return failed
