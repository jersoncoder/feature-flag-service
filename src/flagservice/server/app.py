"""Application factory."""

from __future__ import annotations

from contextlib import closing

from fastapi import FastAPI

from flagservice import __version__
from flagservice.server.db import connect, migrate
from flagservice.server.deps import Conn
from flagservice.server.errors import install_handlers
from flagservice.server.routes import management, runtime
from flagservice.server.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app, applying pending migrations first.

    With no argument, settings come from the environment (see ``Settings.from_env``).
    """
    if settings is None:
        settings = Settings.from_env()
    with closing(connect(settings.database_path)) as conn:
        migrate(conn)

    app = FastAPI(
        title="feature-flag-service",
        version=__version__,
        description="Feature flags with targeting rules, percentage rollouts and an audit log.",
        separate_input_output_schemas=False,
    )
    app.state.settings = settings
    install_handlers(app)
    app.include_router(management.router)
    app.include_router(runtime.router)

    @app.get("/healthz", tags=["health"])
    def health(conn: Conn) -> dict[str, str]:
        conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    return app
