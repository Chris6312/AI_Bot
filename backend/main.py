"""Compatibility entrypoint for local uvicorn launches.

This module deliberately re-exports the canonical FastAPI app from ``app.main`` so
manual launches such as ``uvicorn main:app`` use the same authenticated control
plane, readiness probes, watchlist routes, and lifespan startup logic as the
package entrypoint.
"""

from app.main import app


if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host='0.0.0.0', port=8000)
