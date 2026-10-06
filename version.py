"""Which Backspins this is.

"dev" when run from the source folder. The release workflow writes the tag
here (VERSION = '0.1.4') before packaging, so a downloaded app knows its own
version — which is how it can tell when a newer one is out (updates.py).
"""
VERSION = 'dev'
