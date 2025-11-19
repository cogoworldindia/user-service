from fastapi import FastAPI
from app.db import ensure_database_exists, run_migrations, redis_client
from app.core.config import settings
from contextlib import asynccontextmanager
from app.controllers import router as api_router
import uvicorn

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles startup and shutdown lifecycle events."""
    database_url = settings.DATABASE_URL_SYNC
    if not database_url:
        raise ValueError("DATABASE_URL not found in environment variables.")

    # Ensure DB exists
    ensure_database_exists(database_url)

    # Run Alembic migrations
    run_migrations()

    # Redis initialization 
    # await redis_client.init_redis()

    yield  # App runs while inside this context

    # Shutdown: close Redis connection
    # await redis_client.close_redis()

    print(" Shutting down, cleaning up resources...")


app = FastAPI(
    title="User Service",
    description="Handles user authentication and profile management.",
    version="1.0.0",
    lifespan=lifespan
)


# Include routers
app.include_router(api_router, prefix="/v1/users", tags=["Users"])

# Health check route
@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok", "service": "auth_service"}

# Entry point for running app
if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=int(settings.APP_PORT),  # read from .env or settings
        reload=True
    )