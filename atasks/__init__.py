"""
ATasks package
"""

try:
	from importlib.metadata import version

	__version__ = version('atasks')
except Exception:
	__version__ = '0+unknown'
