"""
PDP Library API

Provides read and write APIs for accessing PDP configuration and policy data.
Users can choose between:
- read_api: Makes HTTP requests to a service
- read_api_from_config: Reads from a YAML config file
"""

# Expose submodules for direct import
from . import read_api
from . import read_api_from_config
from . import write_api
from . import models

__all__ = [
    "read_api",
    "read_api_from_config",
    "write_api",
    "models",
]

# Made with Bob
