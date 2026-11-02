"""Grape kernel: shared foundation modules (Issue #699 Phase 1).

Kernel is the bottom layer: it must not import src.domains.* or src.api.*
(enforced by scripts/check-arch.py R1/R2).
"""
