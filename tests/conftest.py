"""Shared pytest fixtures for the shopping-agent test suite."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from shopping_agent.services.prediction import PurchaseRecord


class FakeScalarsResult:
    """Tiny stand-in for SQLAlchemy scalar result objects used in tests."""

    def __init__(self, values: list[Any]):
        self._values = list(values)

    def all(self) -> list[Any]:
        return list(self._values)

    def first(self) -> Any | None:
        return self._values[0] if self._values else None

    def __iter__(self):
        return iter(self._values)


class FakeResult:
    """Minimal SQLAlchemy-like result wrapper for mocked sessions."""

    def __init__(
        self,
        *,
        rows: list[Any] | None = None,
        scalars: list[Any] | None = None,
        scalar: Any | None = None,
    ) -> None:
        self._rows = list(rows or [])
        self._scalars = list(scalars) if scalars is not None else list(rows or [])
        self._scalar = scalar

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalars(self) -> FakeScalarsResult:
        return FakeScalarsResult(self._scalars)

    def scalar(self) -> Any | None:
        if self._scalar is not None:
            return self._scalar
        if len(self._rows) == 1 and not isinstance(self._rows[0], tuple):
            return self._rows[0]
        return None

    def scalar_one_or_none(self) -> Any | None:
        if self._scalar is not None:
            return self._scalar
        if self._scalars:
            return self._scalars[0]
        if self._rows:
            return self._rows[0]
        return None

    def __iter__(self):
        return iter(self._rows)


class DummyTemplates:
    """Capture template rendering without touching real Jinja output."""

    def __init__(self) -> None:
        self.template_calls: list[tuple[str, dict[str, Any]]] = []
        self.render_calls: list[tuple[str, dict[str, Any]]] = []
        self.env = SimpleNamespace(get_template=self.get_template)

    def get_template(self, name: str):
        parent = self

        class _Template:
            def render(self, **context: Any) -> str:
                parent.render_calls.append((name, context))
                return f"rendered:{name}"

        return _Template()

    def TemplateResponse(self, name: str, context: dict[str, Any]) -> HTMLResponse:
        self.template_calls.append((name, context))
        return HTMLResponse(f"template:{name}")


class AsyncContextManager:
    """Simple async context manager wrapper used to mock async_session()."""

    def __init__(self, value: Any):
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


@pytest.fixture
def sample_purchases() -> list[PurchaseRecord]:
    """Three purchases at regular weekly intervals, 2 units each."""
    return [
        PurchaseRecord(order_date=date(2025, 1, 1), quantity=2),
        PurchaseRecord(order_date=date(2025, 1, 8), quantity=2),
        PurchaseRecord(order_date=date(2025, 1, 15), quantity=2),
    ]


@pytest.fixture
def irregular_purchases() -> list[PurchaseRecord]:
    """Purchases at irregular intervals to test confidence scoring."""
    return [
        PurchaseRecord(order_date=date(2025, 1, 1), quantity=1),
        PurchaseRecord(order_date=date(2025, 1, 20), quantity=3),
        PurchaseRecord(order_date=date(2025, 2, 5), quantity=1),
        PurchaseRecord(order_date=date(2025, 3, 1), quantity=2),
    ]


@pytest.fixture
def fake_result():
    return FakeResult


@pytest.fixture
def dummy_templates() -> DummyTemplates:
    return DummyTemplates()


@pytest.fixture
def make_request():
    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    def _make_request(path: str = "/", method: str = "GET") -> Request:
        return Request(
            {
                "type": "http",
                "method": method,
                "path": path,
                "headers": [],
                "query_string": b"",
            },
            receive=_receive,
        )

    return _make_request


@pytest.fixture
def async_cm():
    return AsyncContextManager
