"""A small local settings UI for editing config/config.yaml.

Serves a single-page form (point ranges to filter on, R-multiples, runner
size, trading times, entry buffer, runner mode, FOMC toggle, safety limits)
backed by two endpoints that reuse the exact same `AppConfig` Pydantic
schema the trading bot validates against at startup - so nothing invalid
can ever be saved, and there is exactly one source of truth for what a
valid config looks like.

Changes are NOT applied to a running session live: `app.py::run_forever`
reloads config.yaml fresh before scheduling each new session, so an edit
here takes effect starting the next session, never mid-trade.

This edits risk/sizing/trading-time parameters, so it is placed behind
HTTP Basic auth and refuses to start without a password configured - see
`main()`. For access from outside the VM, put it behind an SSH tunnel or a
reverse proxy with TLS rather than exposing the port directly.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import secrets as secrets_module
import shutil
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import ValidationError

from orb_bot.config import KNOWN_POINT_VALUES, AppConfig

logger = logging.getLogger("orb_bot.webui")

STATIC_DIR = Path(__file__).parent / "webui_static"


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _dump_yaml_atomic(path: Path, data: dict) -> None:
    backups_dir = path.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(path, backups_dir / f"{path.stem}.{stamp}{path.suffix}")

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
    os.replace(tmp_path, path)


def create_app(config_path: str, webui_username: str, webui_password: str) -> FastAPI:
    app = FastAPI(title="ORB Bot Settings")
    security = HTTPBasic()
    path = Path(config_path)

    def verify_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
        user_ok = secrets_module.compare_digest(credentials.username, webui_username)
        pass_ok = secrets_module.compare_digest(credentials.password, webui_password)
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"}
            )

    @app.get("/")
    def index(_: None = Depends(verify_auth)) -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/meta")
    def meta(_: None = Depends(verify_auth)) -> dict:
        return {"known_point_values": KNOWN_POINT_VALUES}

    @app.get("/api/config")
    def get_config(_: None = Depends(verify_auth)) -> JSONResponse:
        raw = _load_yaml(path)
        try:
            validated = AppConfig.model_validate(raw)
        except ValidationError as exc:
            # The on-disk file is currently invalid (e.g. hand-edited badly).
            # Surface that clearly instead of silently serving stale data.
            raise HTTPException(status_code=500, detail=f"config.yaml is currently invalid: {exc}") from exc
        return JSONResponse(validated.model_dump(mode="json"))

    @app.post("/api/config")
    def post_config(payload: dict[str, Any], _: None = Depends(verify_auth)) -> dict:
        try:
            validated = AppConfig.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_format_validation_errors(exc)) from exc

        _dump_yaml_atomic(path, validated.model_dump(mode="json"))
        logger.info("config.yaml updated via settings UI")
        return {"ok": True}

    return app


def _format_validation_errors(exc: ValidationError) -> list[dict]:
    out = []
    for err in exc.errors():
        out.append({"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]})
    return out


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="ORB bot settings web UI")
    parser.add_argument("--config", default=os.environ.get("ORB_CONFIG_PATH", "config/config.yaml"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    password = os.environ.get("ORB_WEBUI_PASSWORD", "")
    if not password:
        raise SystemExit(
            "ORB_WEBUI_PASSWORD is not set. Refusing to start the settings UI "
            "without a password - it edits live risk/sizing/trading-time "
            "parameters. Set it in your .env and re-run."
        )
    username = os.environ.get("ORB_WEBUI_USERNAME", "admin")

    app = create_app(args.config, username, password)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
