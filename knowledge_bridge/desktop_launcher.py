"""Independent desktop entry point; Obsidian is only an optional editor."""
from __future__ import annotations
import argparse
import os
import webbrowser
from .bridge_launcher import Launcher
from .workspace import PROGRAM_ROOT


def main():
    parser=argparse.ArgumentParser('界渡')
    parser.add_argument('command',nargs='?',default='start',choices=['start','stop','restart'])
    parser.add_argument('--no-open',action='store_true')
    args=parser.parse_args()
    launcher=None
    try:
        launcher=Launcher(PROGRAM_ROOT)
        getattr(launcher,args.command)()
        if args.command!='stop' and not args.no_open:
            webbrowser.open(f"http://127.0.0.1:{launcher.settings()['console']['port']}/")
        return 0
    except Exception as exc:
        if launcher is not None:
            launcher.log(f'Desktop {args.command} failed: {exc}')
        if not args.no_open and os.name=='nt':
            import ctypes
            detail=f'\n\n日志：{launcher.log_path}' if launcher is not None else ''
            ctypes.windll.user32.MessageBoxW(None,f'界渡启动操作未完成：{exc}{detail}','界渡',0x10)
        return 1


if __name__=='__main__':raise SystemExit(main())
