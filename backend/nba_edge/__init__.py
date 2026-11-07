"""NBA Quant Edge backend — Stage 1: storage + odds snapshot pipeline.

Nothing in this package fabricates odds. Rows are only ever written from a real
provider response (or, in tests, from clearly-synthetic fixtures against a scratch
database). See docs/ARCHITECTURE.md and backend/README.md.
"""

__version__ = "0.1.0"
