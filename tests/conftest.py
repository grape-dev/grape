"""Shared fixtures for all tests."""
import pytest
import os
import sys

# Ensure src/ is in path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
