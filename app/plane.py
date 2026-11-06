from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class PlaneClient:
    def __init__(self, base_url: str, workspace_slug: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.workspace_slug = workspace_slug
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5),
            headers={"X-Api-Key": api_key},
        )
        self._project_cache: dict[str, dict] = {}

    async def close(self) -> None:
        await self.http.aclose()

    async def _get_first(
        self, paths: list[str], *, params: dict | None = None
    ) -> dict | None:
        for path in paths:
            try:
                response = await self.http.get(f"{self.base_url}{path}", params=params)
            except httpx.HTTPError as exc:
                logger.warning("Plane API network error path=%s error=%s", path, exc)
                return None
            if response.status_code == 404:
                continue
            if not response.is_success:
                logger.warning(
                    "Plane API request failed path=%s status=%d",
                    path,
                    response.status_code,
                )
                return None
            try:
                body = response.json()
            except ValueError:
                logger.warning("Plane API returned invalid JSON path=%s", path)
                return None
            if isinstance(body, dict):
                return body
            logger.warning("Plane API returned unexpected response path=%s", path)
            return None
        logger.warning("Plane API resource not found tried_paths=%d", len(paths))
        return None

    async def get_work_item(self, project_id: str, work_item_id: str) -> dict | None:
        slug = quote(self.workspace_slug, safe="")
        project = quote(project_id, safe="")
        item = quote(work_item_id, safe="")
        paths = [
            f"/api/v2/workspaces/{slug}/projects/{project}/work-items/{item}/",
            f"/api/v1/workspaces/{slug}/projects/{project}/work-items/{item}/",
            f"/api/v1/workspaces/{slug}/projects/{project}/issues/{item}/",
        ]
        result = await self._get_first(paths, params={"expand": "labels"})
        if result is None:
            result = await self._get_first(paths)
        return result

    async def get_project(self, project_id: str) -> dict | None:
        if project_id in self._project_cache:
            return self._project_cache[project_id]
        slug = quote(self.workspace_slug, safe="")
        project = quote(project_id, safe="")
        result = await self._get_first(
            [
                f"/api/v2/workspaces/{slug}/projects/{project}/",
                f"/api/v1/workspaces/{slug}/projects/{project}/",
            ]
        )
        if result:
            self._project_cache[project_id] = result
        return result

    async def enrich_work_item(self, data: dict) -> dict:
        result = dict(data)
        project_id = str(data.get("project_id") or "")
        work_item_id = str(data.get("id") or "")

        if project_id and work_item_id:
            work_item = await self.get_work_item(project_id, work_item_id)
            if work_item:
                webhook_name = result.get("name")
                result = {**work_item, **result}
                if webhook_name in (None, "", "Задача в Plane"):
                    result["name"] = work_item.get("name") or webhook_name
                logger.info(
                    "Enriched notification with Plane work item data work_item_id=%s",
                    work_item_id,
                )

        if project_id:
            project = await self.get_project(project_id)
            if project and project.get("name"):
                result["project_name"] = project["name"]

        return result
