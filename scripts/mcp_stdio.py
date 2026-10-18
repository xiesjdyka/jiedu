"""Absolute-path STDIO entry point; never depends on the caller's cwd."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from knowledge_bridge.cli import main

if __name__=='__main__':
    sys.argv=[sys.argv[0],'mcp','--transport','stdio']
    main()
