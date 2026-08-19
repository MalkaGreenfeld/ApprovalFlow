"""FastAPI middleware that reads/propagates the correlation id.

Usage::

    from approvalflow.middleware import CorrelationIdMiddleware
    app.add_middleware(CorrelationIdMiddleware)
"""

from __future__ import annotations

from uuid import UUID, uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .logging import set_correlation_id

HEADER = "X-Correlation-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        cid_raw = request.headers.get(HEADER, str(uuid4()))

        # Validate UUID
        try:
            cid = str(UUID(cid_raw))
        except ValueError:
            cid = str(uuid4())

        set_correlation_id(cid)
        request.state.correlation_id = cid

        response: Response = await call_next(request)
        response.headers[HEADER] = cid
        return response
