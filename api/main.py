# Aftershock — API: FastAPI app (§10.1)

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import ask, changesets, health, impact, replay, webhooks
from core.config import get_settings

logging.basicConfig(level=get_settings().log_level)

app = FastAPI(title="Aftershock API", version="0.1.0")

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(ask.router)
app.include_router(webhooks.router)
app.include_router(changesets.router)
app.include_router(impact.router)
app.include_router(replay.router)
