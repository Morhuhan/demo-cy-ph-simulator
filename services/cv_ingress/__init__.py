"""Приём событий реального CV-модуля."""

from .service import SERVICE_NAME, CvIngressService, create_router

__all__ = ["SERVICE_NAME", "CvIngressService", "create_router"]
