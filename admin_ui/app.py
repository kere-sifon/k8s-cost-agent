"""FastAPI admin UI for registering and managing target clusters.

Localhost-bound by default. No auth layer at this MVP scope — see README.
Tokens are write-only: never rendered back into templates once saved.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.status import HTTP_303_SEE_OTHER

from admin_ui import db
from admin_ui.rbac_verify import verify_and_persist

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("admin-ui")

TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(
    title="k8s-cost-agent admin",
    description="Local cluster registration UI (no query/chat — MCP only).",
    docs_url=None,
    redoc_url=None,
)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    logger.info("admin-ui ready | db=%s", db.get_db_path())


def _flash_redirect(url: str, *, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    params: list[str] = []
    if error:
        params.append(f"error={quote(error)}")
    if notice:
        params.append(f"notice={quote(notice)}")
    if params:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{'&'.join(params)}"
    return RedirectResponse(url=url, status_code=HTTP_303_SEE_OTHER)


@app.get("/", response_class=HTMLResponse)
def cluster_list(request: Request) -> HTMLResponse:
    clusters = db.list_clusters()
    return templates.TemplateResponse(
        request,
        "clusters/list.html",
        {
            "clusters": clusters,
            "error": request.query_params.get("error"),
            "notice": request.query_params.get("notice"),
        },
    )


@app.get("/clusters/new", response_class=HTMLResponse)
def new_cluster_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "clusters/form.html",
        {
            "mode": "new",
            "cluster": None,
            "error": request.query_params.get("error"),
        },
    )


@app.post("/clusters/new")
def create_cluster(
    name: str = Form(...),
    api_server_url: str = Form(...),
    sa_token: str = Form(...),
    ca_cert_pem: str = Form(""),
    kubecost_url: str = Form(""),
    kubecost_token: str = Form(""),
) -> RedirectResponse:
    name = name.strip()
    api_server_url = api_server_url.strip()
    sa_token = sa_token.strip()
    if not name or not api_server_url or not sa_token:
        return _flash_redirect(
            "/clusters/new",
            error="Name, API server URL, and token are required.",
        )
    if db.get_cluster_by_name(name):
        return _flash_redirect(
            "/clusters/new",
            error="A cluster with that name already exists.",
        )
    try:
        cluster = db.create_cluster(
            name=name,
            api_server_url=api_server_url,
            sa_token=sa_token,
            kubecost_url=kubecost_url or None,
            kubecost_token=kubecost_token or None,
            ca_cert_pem=ca_cert_pem or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("create_cluster failed")
        return _flash_redirect("/clusters/new", error=str(exc))
    return _flash_redirect("/", notice=f"Registered cluster {cluster.name}")


@app.get("/clusters/{cluster_id}/edit", response_class=HTMLResponse)
def edit_cluster_form(request: Request, cluster_id: int) -> HTMLResponse:
    cluster = db.get_cluster(cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return templates.TemplateResponse(
        request,
        "clusters/form.html",
        {
            "mode": "edit",
            "cluster": cluster,
            "error": request.query_params.get("error"),
        },
    )


@app.post("/clusters/{cluster_id}/edit")
def edit_cluster(
    cluster_id: int,
    name: str = Form(...),
    api_server_url: str = Form(...),
    sa_token: str = Form(""),
    ca_cert_pem: str = Form(""),
    kubecost_url: str = Form(""),
    kubecost_token: str = Form(""),
) -> RedirectResponse:
    if db.get_cluster(cluster_id) is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    name = name.strip()
    api_server_url = api_server_url.strip()
    if not name or not api_server_url:
        return _flash_redirect(
            f"/clusters/{cluster_id}/edit",
            error="Name and API server URL are required.",
        )
    other = db.get_cluster_by_name(name)
    if other and other.id != cluster_id:
        return _flash_redirect(
            f"/clusters/{cluster_id}/edit",
            error="A cluster with that name already exists.",
        )
    try:
        db.update_cluster(
            cluster_id,
            name=name,
            api_server_url=api_server_url,
            sa_token=sa_token or None,
            kubecost_url=kubecost_url or None,
            kubecost_token=kubecost_token or None,
            ca_cert_pem=ca_cert_pem,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("update_cluster failed")
        return _flash_redirect(f"/clusters/{cluster_id}/edit", error=str(exc))
    return _flash_redirect("/", notice="Cluster updated")


@app.post("/clusters/{cluster_id}/delete")
def delete_cluster(cluster_id: int) -> RedirectResponse:
    deleted = db.delete_cluster(cluster_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return _flash_redirect("/", notice="Cluster deleted")


@app.post("/clusters/{cluster_id}/verify")
def verify_cluster(cluster_id: int) -> RedirectResponse:
    if db.get_cluster(cluster_id) is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    result = verify_and_persist(cluster_id)
    if result.ok:
        return _flash_redirect("/", notice="RBAC verified")
    return _flash_redirect("/", notice="RBAC verification failed — see status on the row")


def main() -> None:
    import uvicorn

    host = os.environ.get("ADMIN_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("ADMIN_UI_PORT", "8082"))
    uvicorn.run("admin_ui.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
