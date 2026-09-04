"""Installed command wrappers for the Python runtime."""

from __future__ import annotations

from .cli import main


def play() -> int:
    return main(["play"])


def co_create() -> int:
    return main(["co-create"])


def validate() -> int:
    return main(["validate"])


def evaluate_live() -> int:
    return main(["evaluate-live"])
