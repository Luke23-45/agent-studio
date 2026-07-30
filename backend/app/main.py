"""
Neryva Agent Studio - Backend Application

This is the main entry point for the FastAPI application.
"""

from fastapi import FastAPI

from backend.app.api.routes import conversations_router
from backend.app.settings.env import settings


def create_application() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Neryva Agent Studio",
        description="Governance and control layer for LLM applications",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Register routes
    app.include_router(conversations_router, prefix="/api/v1")

    @app.get("/health")
    async def health_check():
        return {"status": "healthy"}

    return app


app = create_application()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )