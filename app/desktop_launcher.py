"""Cross-platform launcher for the complete main app and judge workspace."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import webbrowser

import desktop_entry
import downloadable_demo
import install_update
import operations_server
import browser_proposals


def data_directory(main=False):
    if main and os.name == 'nt':
        return Path.home() / 'AgentRehearsal' / 'operations_runs'
    return desktop_entry.private_root() / ('main-workspace' if main else 'judge-workspace')


def prepare(main, directory, port):
    if not main:
        return downloadable_demo.prepare_demo(directory, port=port)
    _, version = install_update.payload(Path(__file__).resolve().parent)
    # Constructing a client makes no model request. The same browser reviews and
    # caps apply to owner-configured access and privately entered session keys.
    key = os.environ.get('NEBIUS_API_KEY', '').strip()
    providers = browser_proposals.providers(key) if key else None
    server = operations_server.Server(directory, port=port, proposal_providers=providers)
    return server, None, version


def open_workspace(server, guide):
    url = guide.as_uri() if guide else 'http://127.0.0.1:' + str(server.server_port)
    try:
        return webbrowser.open(url)
    except (OSError, webbrowser.Error):
        return False


def console(args):
    directory = args.directory or data_directory(args.main)
    if args.check:
        result = downloadable_demo.check_demo(directory)
        print(json.dumps(result), flush=True)
        return 0 if result['status'] == 'PASSED' else 1
    server, guide, version = prepare(args.main, directory, args.port)
    print('Orkestryn ' + version, flush=True)
    print('Workspace: http://127.0.0.1:' + str(server.server_port), flush=True)
    if guide:
        print('Private welcome page: ' + str(guide), flush=True)
    print('Private data: ' + str(Path(directory).absolute()), flush=True)
    print('Startup made zero model requests. Keep this launcher running.', flush=True)
    print('Stop the observer and owned workers in Monitoring before closing this launcher.', flush=True)
    try:
        if not args.no_browser:
            open_workspace(server, guide)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def graphical(args):
    import tkinter as tk
    from tkinter import ttk, messagebox
    window = tk.Tk()
    window.title('Orkestryn')
    window.geometry('620x400')
    window.minsize(530, 360)
    frame = ttk.Frame(window, padding=28)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Orkestryn', font=('Segoe UI', 22, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='Agents in sync. Humans in control.').pack(anchor='w', pady=(2, 20))
    status = tk.StringVar(value='Choose a workspace. The complete dashboard opens in your browser.')
    ttk.Label(frame, textvariable=status, wraplength=550).pack(anchor='w', pady=(0, 18))
    state = {'server': None, 'guide': None, 'thread': None}

    def start(main):
        try:
            server, guide, version = prepare(main, args.directory or data_directory(main), args.port)
        except (ValueError, OSError, KeyError):
            messagebox.showerror('Could not start', 'Use a writable private data folder and a complete verified download. No credentials were printed.')
            return
        state.update(server=server, guide=guide)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        state['thread'] = thread
        thread.start()
        for button in (judge, owner):
            button.configure(state='disabled')
        reopen.configure(state='normal')
        status.set(('Main workspace' if main else 'Judge evaluation') + ' · ' + version + '\nhttp://127.0.0.1:' + str(server.server_port) + '\n\nKeep this window open. Startup made zero model requests.')
        if not args.no_browser:
            open_workspace(server, guide)

    judge = ttk.Button(frame, text='Open judge evaluation', command=lambda: start(False))
    judge.pack(fill='x', pady=4)
    owner = ttk.Button(frame, text='Open main workspace', command=lambda: start(True))
    owner.pack(fill='x', pady=4)
    reopen = ttk.Button(frame, text='Reopen browser', state='disabled', command=lambda: open_workspace(state['server'], state['guide']))
    reopen.pack(fill='x', pady=4)
    ttk.Label(frame, text='Before closing: stop the observer and owned workers in Monitoring.\nJudges use the generated private welcome page. Keys are never bundled.', wraplength=550).pack(anchor='w', pady=(18, 0))

    def close():
        server = state['server']
        if server:
            if not messagebox.askokcancel('Close workspace', 'Stop the observer and owned workers in Monitoring first. Closing stops this web server; it does not approve worker shutdown. Any remaining leased workers expire at their fixed deadline. Close now?'):
                return
            server.shutdown()
            server.server_close()
        window.destroy()

    window.protocol('WM_DELETE_WINDOW', close)
    if args.main:
        window.after(0, lambda: start(True))
    window.mainloop()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--main', action='store_true', help='Use the owner workspace; Windows reuses ~/AgentRehearsal/operations_runs.')
    parser.add_argument('--console', action='store_true', help='Run without the optional graphical launcher.')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--check', action='store_true', help='Run one finite free local walkthrough and exit.')
    args = parser.parse_args(argv)
    if args.port and not 1 <= args.port <= 65535:
        parser.error('Use a port from 1 to 65535, or 0 to choose a free local port.')
    try:
        if args.console or args.check:
            return console(args)
        try:
            import tkinter
        except ImportError:
            return console(args)
        try:
            return graphical(args)
        except tkinter.TclError:
            return console(args)
    except (ValueError, OSError, KeyError):
        print('Orkestryn could not start. Use a complete verified download and a writable private data folder. Credentials were not printed.', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
