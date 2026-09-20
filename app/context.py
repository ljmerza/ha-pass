"""Shared template context builder.

Extracted from main.py so routers can import it without a circular dependency.
"""
from fastapi import Request

from app.build import BUILD_VERSION
from app.config import settings
from app.theme import brand_bg_dark, brand_css


def base_context(request: Request) -> dict:
    """Common template context: theme, CSP nonce, ingress base path.

    ``build_version`` is here rather than in each route because every
    template emits static asset URLs and a page that missed it would keep
    serving the previous build's assets while looking fine.
    """
    return {
        "request": request,
        "app_name": settings.app_name,
        "brand_bg": settings.brand_bg,
        "brand_bg_dark": brand_bg_dark,
        "brand_primary": settings.brand_primary,
        "brand_css": brand_css,
        "csp_nonce": request.state.csp_nonce,
        "base_path": request.state.ingress_path,
        "build_version": BUILD_VERSION,
    }
