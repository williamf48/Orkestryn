#!/usr/bin/env python3
"""Start the main workspace, or an isolated judge demo with --demo."""
from pathlib import Path
import sys

def main():
    app=Path(__file__).resolve().parent/'app'
    sys.path.insert(0,str(app))
    from desktop_launcher import main as launch
    args=sys.argv[1:]
    demo='--demo' in args
    if demo and '--main' in args:
        raise SystemExit('Choose --main or --demo, not both.')
    if demo:
        args.remove('--demo')
    elif '--main' not in args:
        args.insert(0,'--main')
    return launch(args)

if __name__=='__main__':
    raise SystemExit(main())
