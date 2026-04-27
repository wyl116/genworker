"""
Application entry point for genworker.

Loads configuration and starts the uvicorn server.
"""
import os
import sys


def main():
    """Start the genworker server."""
    # Ensure project root is in Python path
    project_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_root)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # Load environment configuration
    try:
        from src.common.settings import load_layered_env

        load_layered_env()
    except ImportError:
        print("Warning: python-dotenv not installed, using env vars only")
    env = os.getenv("ENVIRONMENT") or os.getenv("ENV", "local")

    # Read server settings from environment
    host = os.getenv("HTTP_HOST", "0.0.0.0")
    port = int(os.getenv("HTTP_PORT", "8000"))
    workers = int(os.getenv("HTTP_WORKERS", "1"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()

    print(f"Starting genworker server on {host}:{port} (env={env})")

    import uvicorn
    uvicorn.run(
        "src.api.app:app",
        host=host,
        port=port,
        workers=workers,
        log_level=log_level,
    )


if __name__ == "__main__":
    main()
