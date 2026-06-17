#!/usr/bin/env python3
"""Wrapper d'exécution.

Usage :
    python run.py --test-connection
    python run.py --audit
    python run.py --dedup                 # DRY-RUN (ne modifie rien)
    python run.py --dedup --apply         # archive les doublons à traiter
"""

from pb_audit.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
