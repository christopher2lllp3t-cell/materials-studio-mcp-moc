from .version_source import release_identity


__version__ = release_identity()["version"]

__all__ = ["__version__"]
