"""Pydantic v2 wire format for ``config/tools/<name>.yaml`` files.

Loader semantics (see ADR-02 §4):
    * One YAML file per tool. Filename stem must equal the ``name`` field.
    * ``class_path`` is ``module.path:ClassName`` and is imported lazily by
      the registry.
    * ``parameters`` is OPTIONAL — if absent, the registry derives the
      JSON Schema from the Tool class itself (``Parameters`` Pydantic
      model in S3.4, or legacy ``parameters_schema`` dict for now).
    * ``extra="forbid"`` so a typo in the YAML fails loud at boot.
"""
from __future__ import annotations

import importlib
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict

from agents.tools.base import BaseTool

# Audience is the access-control surface from ADR-02 §4. Public dashboards,
# clinician tools, and admin-only tools are the three buckets we recognise
# today; extend the Literal when a new audience is introduced.
Audience = Literal["public", "clinician", "admin"]


class ToolSpec(BaseModel):
    """One YAML file = one ToolSpec."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str
    description_th: str
    description_en: Optional[str] = None
    class_path: str  # "module.path:ClassName"
    enabled: bool = True
    audience: List[Audience] = ["public", "clinician"]
    # Optional override of the JSON Schema. When absent the registry
    # introspects the Tool class (Parameters Pydantic model preferred,
    # falling back to the legacy ``parameters_schema`` dict).
    parameters: Optional[Dict[str, Any]] = None


def import_tool_class(class_path: str) -> type[BaseTool]:
    """Resolve ``"module.path:ClassName"`` to a ``BaseTool`` subclass.

    Fails loud on:
        * malformed ``class_path`` (no ``:`` separator)
        * import error
        * missing attribute on the module
        * attribute is not a subclass of ``BaseTool``
    """
    if ":" not in class_path:
        raise ValueError(
            f"tool class_path must be 'module.path:ClassName', got {class_path!r}"
        )
    module_name, _, attr = class_path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(
            f"failed to import tool module {module_name!r}: {exc}"
        ) from exc
    try:
        cls = getattr(module, attr)
    except AttributeError as exc:
        raise AttributeError(
            f"module {module_name!r} has no attribute {attr!r}"
        ) from exc
    if not isinstance(cls, type) or not issubclass(cls, BaseTool):
        raise TypeError(
            f"{class_path} resolves to {cls!r} which is not a BaseTool subclass"
        )
    return cls
