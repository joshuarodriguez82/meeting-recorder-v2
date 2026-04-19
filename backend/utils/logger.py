"""Structured application logger."""

import logging
import sys


def get_logger(name: str) -> logging.Logger:
    """
    Return a configured logger for the given module name.

    Args:
        name: Typically __name__ from the calling module.

    Returns:
        Configured Logger instance.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger