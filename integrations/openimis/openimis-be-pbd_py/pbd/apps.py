"""Django AppConfig following the openIMIS module convention."""

from __future__ import annotations

import contextlib

from django.apps import AppConfig

from .config import DEFAULT_CFG, configure

MODULE_NAME = "pbd"


class PbdConfig(AppConfig):
    name = MODULE_NAME
    verbose_name = "Privacy-by-Design control plane enforcement"

    def ready(self) -> None:
        cfg = dict(DEFAULT_CFG)
        with contextlib.suppress(Exception):  # core absent (tests) or table not migrated yet
            from core.models import ModuleConfiguration  # type: ignore[import-not-found]

            db_cfg = ModuleConfiguration.get_or_default(MODULE_NAME, DEFAULT_CFG)
            if isinstance(db_cfg, dict):
                cfg.update(db_cfg)
        configure(cfg)
        from . import signals

        signals.bind()
