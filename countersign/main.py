"""Entry point: uvicorn countersign.main:app"""

from .app import create_app

app = create_app()
