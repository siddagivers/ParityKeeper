"""ParityKeeper Windows desktop GUI. Requires Python 3.10+ (Tkinter included)."""
from __future__ import annotations

import csv
import json
import logging
from logging.handlers import RotatingFileHandler
import functools
import progress as progresslib
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import traceback

import engine

APPDIR = Path(os.getenv('APPDATA', str(Path.home()))) / 'ParityKeeper'
SETTINGS = APPDIR / 'settings.json'
DEFAULTS = dict(root='', mirror='', exe='', small_pct=12, large_pct=8,
                small_mb=16, group_mb=256, max_files=256, small_threads=6,
                large_threads=2, rates={})


class HoverHelp:
    """Hover help for any standard Tk/ttk widget; disappears on click/leave."""
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.popup = None
        self.pending = None
        widget.bind('<Enter>', self._enter, add='+')
        widget.bind('<Leave>', self._leave, add='+')
        widget.bind('<ButtonPress>', self._leave, add='+')
        widget.bind('<Destroy>', self._leave, add='+')

    def _enter(self, event=None):
        self._cancel()
        self.pending = self.widget.after(650, self._show)

    def _cancel(self):
        if self.pending is not None:
            try:
                self.widget.after_cancel(self.pending)
            except tk.TclError:
                pass
            self.pending = None

    def _leave(self, event=None):
        self._cancel()
        if self.popup is not None:
            self.popup.destroy()
            self.popup = None

    def _show(self):
        self.pending = None
        if not self.widget.winfo_exists():
            return
        self.popup = tk.Toplevel(self.widget)
        self.popup.wm_overrideredirect(True)
        self.popup.attributes('-topmost', True)
        label = tk.Label(self.popup, text=self.text, justify='left', wraplength=390,
                         background='#fffbd8', foreground='#1b2730',
                         relief='solid', borderwidth=1, padx=9, pady=6,
                         font=('Segoe UI', 9))
        label.pack()
        self.popup.update_idletasks()
        x = min(self.widget.winfo_pointerx() + 14,
                self.widget.winfo_screenwidth() - self.popup.winfo_reqwidth() - 12)
        y = min(self.widget.winfo_pointery() + 20,
                self.widget.winfo_screenheight() - self.popup.winfo_reqheight() - 12)
        self.popup.geometry(f'+{max(0,x)}+{max(0,y)}')



def find_multipar() -> str:
    spots = [
        Path('C:/Program Files/MultiPar/par2j64.exe'),
        Path('C:/Program Files (x86)/MultiPar/par2j64.exe'),
        Path('C:/Program Files/MultiPar/par2j.exe'),
        Path('C:/Program Files (x86)/MultiPar/par2j.exe'),
    ]
    for p in spots:
        if p.exists():
            return str(p)
    return ''


class Window(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('ParityKeeper — Windows PAR2 Archive Guardian  |  v1.2')
        self.geometry('1250x875')
        self.minsize(940, 660)
        try:
            data = json.loads(SETTINGS.read_text('utf-8'))
        except (OSError, ValueError):
            data = {}
        data = {**DEFAULTS, **data}
        if not data['exe']:
            data['exe'] = find_multipar()
        self.root_var = tk.StringVar(value=data['root'])
        self.mirror_var = tk.StringVar(value=data['mirror'])
        self.exe_var = tk.StringVar(value=data['exe'])
        self.small_pct = tk.StringVar(value=str(data['small_pct']))
        self.large_pct = tk.StringVar(value=str(data['large_pct']))
        self.small_mb = tk.StringVar(value=str(data['small_mb']))
        self.group_mb = tk.StringVar(value=str(data['group_mb']))
        self.max_files = tk.StringVar(value=str(data['max_files']))
        self.small_threads = tk.IntVar(value=max(1, min(10, int(data['small_threads']))))
        self.large_threads = tk.IntVar(value=max(1, min(10, int(data['large_threads']))))
        self.live_threads_lock = threading.Lock()
        self.live_threads = {'small': self.small_threads.get(), 'large': self.large_threads.get()}
        self.logger = logging.getLogger(f'paritykeeper.gui.{id(self)}')
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        try:
            APPDIR.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(APPDIR / 'activity.log', maxBytes=5_000_000,
                                          backupCount=3, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            self.logger.addHandler(handler)
        except OSError:
            pass
        self.rates = data.get('rates', {}) if isinstance(data.get('rates', {}), dict) else {}
        self.latest_progress = None
        self.eta_label = tk.StringVar(value='Hover over buttons and settings for help.')
        self.hints = []
        self.info = tk.StringVar(value='Choose the main backup folder. Each disk has its own inventory.')
        self.status = tk.StringVar(value='Ready')
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.changes: list[engine.Item] = []
        self.issues: list[engine.Item] = []
        self.comparison: list[engine.Item] = []
        self.trees = {}
        self.page_size = 2000
        self.pages = {}
        self.filters = {}
        self.views = {}
        self.page_labels = {}
        self._build()
        self.after(100, self._poll)
        self.protocol('WM_DELETE_WINDOW', self._close)
        if self.root_var.get():
            self.after(300, self._refresh_stats)

    def _help(self, widget, message):
        self.hints.append(HoverHelp(widget, message))
        return widget

    def _thread_changed(self, kind: str, value: str):
        n = max(1, min(10, int(round(float(value)))))
        with self.live_threads_lock:
            self.live_threads[kind] = n
        # Changing a slider cannot affect the currently running MultiPar process.
        # It does affect the next PAR subprocess in the same long-running job.
        if not self.busy:
            self._save()

    def _thread_options(self):
        opts = self._opts()
        opts['thread_provider'] = self._live_thread_count
        return opts

    def _threads_only(self):
        return {'thread_provider': self._live_thread_count}

    def _live_thread_count(self, kind):
        with self.live_threads_lock:
            return self.live_threads[kind]

    def _estimate_text(self, bytes_work: int, title: str) -> str:
        rate = float(self.rates.get(title, 0) or 0)
        if rate > 0:
            return (f'Estimated time from previous measured throughput: '
                    f'{progresslib.duration(bytes_work/rate)}. '
                    'A live ETA will adjust to actual speed.')
        return ('Time remaining will be estimated from actual processing speed '
                'once the operation begins.')

    def _learn_rate(self):
        p = self.latest_progress
        if p and p['elapsed'] >= 1 and p['done'] > 0 and p['speed'] > 0:
            self.rates[self.current_task] = p['speed']
            self._save()

    def _build(self):
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill='both', expand=True)
        head = ttk.LabelFrame(outer, text='Locations  (do not change during an operation)', padding=8)
        head.pack(fill='x')
        head.columnconfigure(1, weight=1)
        self._help(ttk.Label(head, text='Backup folder:'),
                   'Folder to protect, including every subfolder. The .paritykeeper inventory and PAR2 files are stored here.').grid(row=0, column=0, sticky='w', padx=(0, 5))
        self._help(ttk.Entry(head, textvariable=self.root_var),
                   'The primary archive you want to scan or repair. Do not point at a system folder; use your backup root.').grid(row=0, column=1, sticky='ew')
        self._help(ttk.Button(head, text='Browse…', command=lambda: self._choose_dir(self.root_var)),
                   'Select your primary HDD backup folder.').grid(row=0, column=2, padx=5)
        self._help(ttk.Label(head, text='Mirror folder:'),
                   'Optional second archive/HDD. Compare Mirror compares the two saved inventories; it does not copy or delete files.').grid(row=1, column=0, sticky='w', pady=(5, 0))
        self._help(ttk.Entry(head, textvariable=self.mirror_var),
                   'Your independent mirror backup, e.g. the 8 TB HDD. Run Scan/Protect separately on each drive.').grid(row=1, column=1, sticky='ew', pady=(5, 0))
        self._help(ttk.Button(head, text='Browse…', command=lambda: self._choose_dir(self.mirror_var)),
                   'Choose another folder or HDD to compare to the primary archive.').grid(row=1, column=2, padx=5, pady=(5, 0))
        self._help(ttk.Label(head, text='MultiPar CLI:'), 'Path to MultiPar par2j64.exe; MultiPar performs PAR2 creation/verification/repair.').grid(row=2, column=0, sticky='w', pady=(5, 0))
        self._help(ttk.Entry(head, textvariable=self.exe_var),
                   r'Path to par2j64.exe (not MultiPar.exe). For example C:\Program Files\MultiPar\par2j64.exe').grid(row=2, column=1, sticky='ew', pady=(5, 0))
        self._help(ttk.Button(head, text='Locate par2j64.exe…', command=self._choose_exe),
                   'Browse for the MultiPar command-line recovery engine.').grid(row=2, column=2, padx=5, pady=(5, 0))
        self._help(ttk.Button(head, text='Test MultiPar', command=self.test_par),
                   'Self-test using disposable files: create PAR2, verify, corrupt a file, repair, then verify SHA256 again. Does not touch your archive.').grid(row=2, column=3, padx=5, pady=(5, 0))

        prefs = ttk.LabelFrame(outer, text='Recovery settings (used for new/rebuilt sets only)', padding=7)
        prefs.pack(fill='x', pady=(8, 5))
        fields = [
            ('Small PAR %', self.small_pct, 'PAR2 redundancy for groups of small files. A grouped set is protected as a unit. Higher values use more disk capacity.'),
            ('Large PAR %', self.large_pct, 'PAR2 recovery percentage per large file. An 8% PAR2 file can recover roughly 8% missing/corrupt blocks, not a completely missing large file.'),
            ('Small file under MiB', self.small_mb, 'Files smaller than this size are packed into bounded PAR2 groups. Larger files get individual recovery sets.'),
            ('Group limit MiB', self.group_mb, 'Maximum combined source size for one group of small files. Affects grouping of NEW files; old groups are never silently regrouped.'),
            ('Max files/group', self.max_files, 'Maximum number of small files in one PAR2 group. Lower values isolate damage and make repairs more focused.')]
        for ix, (label, var, helptext) in enumerate(fields):
            sub = ttk.Frame(prefs)
            sub.grid(row=0, column=ix, padx=8, sticky='w')
            self._help(ttk.Label(sub, text=label), helptext).pack(anchor='w')
            self._help(tk.Spinbox(sub, from_=1, to=4096, increment=1, textvariable=var,
                                  width=12), helptext).pack(anchor='w')
        threading_bar = ttk.Frame(prefs)
        threading_bar.grid(row=1, column=0, columnspan=5, sticky='ew', pady=(9, 0))
        small_panel = ttk.Frame(threading_bar)
        small_panel.pack(side='left', padx=(7, 42))
        large_panel = ttk.Frame(threading_bar)
        large_panel.pack(side='left')
        for kind, panel, var, default_help in [
            ('small', small_panel, self.small_threads,
             '1–10 CPU threads used INSIDE MultiPar for grouped small files, during creation and verification/repair. 6 is a suggested starting point, not 6 parallel processes.'),
            ('large', large_panel, self.large_threads,
             '1–10 CPU threads used INSIDE MultiPar for single large-file sets, during creation and verification/repair. Fewer threads may be appropriate for HDD I/O.')]:
            self._help(ttk.Label(panel, text=f'{kind.title()}-file MultiPar CPU threads (1–10)'), default_help).pack(anchor='w')
            self._help(tk.Scale(panel, variable=var, from_=1, to=10, resolution=1,
                                showvalue=True, orient='horizontal', length=250,
                                command=lambda value, k=kind: self._thread_changed(k, value)),
                       default_help + ' Changing the slider while running applies to the NEXT MultiPar subprocess, not one already started.').pack(anchor='w')
        self._help(ttk.Label(threading_bar, text='Slider changes take effect on the next MultiPar subprocess, even within the same batch.',
                             foreground='#536579'),
                   'MultiPar does not expose a live CPU-thread adjustment for an already-running process. Each set uses /lcN at launch.').pack(side='left', padx=10)
        ttk.Label(outer, textvariable=self.info, foreground='#215a80').pack(fill='x', pady=(1, 6))

        actions = ttk.Frame(outer)
        actions.pack(fill='x', pady=(2, 6))
        primary = [
            ('1  Scan for changes', self.scan, 'Quickly list NEW, CHANGED and MISSING paths by file size/time. No full disk checksum: corruption can hide behind unchanged metadata.'),
            ('2  Apply checked changes', self.apply, 'After approving items, calculate SHA256+CRC32, create/verify PAR2 and update the inventory. Changed/deleted entries require explicit confirmation.'),
            ('3  CRC / SHA256 check', self.check, 'Read every tracked file fully and compare saved CRC32 and SHA256. This detects bitrot. Shows measured-speed ETA and issues as found.'),
            ('4  Repair checked files', self.repair, 'Restore chosen damaged/missing files through MultiPar. MultiPar repairs complete PAR2 sets; all damaged files in a set must be checked.'),
            ('Verify PAR sets', self.verify_par, 'Ask MultiPar to check recovery sets including recovery-volume integrity. Does not repair data.'),
            ('Rebuild PAR sets', self.rebuild_par, 'Recreate selected missing/damaged PAR2 sets ONLY if all source files still match the original SHA256 baseline.'),
            ('Compare mirror', self.compare, 'Compare the saved SHA256 inventories of your two backup drives. Read-only; not a live file-content scan.'),
        ]
        self.buttons = []
        for title, command, hint in primary:
            b = self._help(ttk.Button(actions, text=title, command=command), hint)
            b.pack(side='left', padx=(0, 5))
            self.buttons.append(b)
        self.stop_btn = self._help(ttk.Button(actions, text='STOP', command=self.cancel.set, state='disabled'),
                                    'Request cancellation. A running MultiPar subprocess is terminated; already COMMITTED sets remain protected.')
        self.stop_btn.pack(side='right')

        statusbar = ttk.Frame(outer)
        statusbar.pack(side='bottom', fill='x', pady=(6, 0))
        self.progress = self._help(ttk.Progressbar(statusbar, mode='determinate', value=0, length=165),
                                   'For checksum operations this is calculated from actual bytes read. For MultiPar it is estimated from group sizes and percentages reported by MultiPar.')
        self.progress.pack(side='left', padx=(0, 9))
        self._help(ttk.Label(statusbar, textvariable=self.status),
                   'Current operation stage or completion result.').pack(side='left', fill='x', expand=True)
        self._help(ttk.Button(statusbar, text='Review leftover PAR2', command=self.review_orphans),
                   'Show PAR2 directories that have no inventory record, often left after an interrupted run. No files are deleted automatically.').pack(side='right', padx=5)
        self._help(ttk.Button(statusbar, text='Export current tab to CSV', command=self.export_csv),
                   "Save the selected tab\'s results (including paths and statuses) in a CSV report.").pack(side='right')
        self._help(ttk.Label(outer, textvariable=self.eta_label, foreground='#215a80'),
                   'Estimated remaining time, amount of read/computational work and effective speed. Estimate improves as the program measures progress; PAR2 workload is approximate.').pack(side='bottom', fill='x', pady=(3, 0))


        self.notebook = self._help(ttk.Notebook(outer), 'Changes: detected files. Integrity: checksum failures and recovery sets. Mirror: inventory differences.')
        self.notebook.pack(fill='both', expand=True)
        self._add_tree('Changes', 'changes', 'NEW / CHANGED / MISSING: select which changes are intentional')
        self._add_tree('Integrity / Repair', 'issues', 'Full CRC32 + SHA256 issues; repair selections are applied by complete PAR groups')
        self._add_tree('Mirror Differences', 'comparison', 'Read-only comparison of saved inventories; not a live disk checksum check')
        logframe = ttk.LabelFrame(outer, text='Activity log', padding=(6, 4))
        logframe.pack(fill='both', expand=False, pady=(7, 0))
        self.log = self._help(tk.Text(logframe, height=6, wrap='word', state='disabled', font=('Consolas', 9)),
                              'Live MultiPar output, errors, file operations and progress. Useful for diagnostics.')
        sblog = ttk.Scrollbar(logframe, command=self.log.yview)
        self.log.configure(yscrollcommand=sblog.set)
        self.log.pack(side='left', fill='both', expand=True)
        sblog.pack(side='right', fill='y')

    def _add_tree(self, title: str, key: str, subtitle: str):
        frame = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(frame, text=title)
        ttk.Label(frame, text=subtitle, foreground='#505050').pack(anchor='w')
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill='x', pady=5)
        self._help(ttk.Button(toolbar, text='Check matching', command=lambda k=key: self._set_all(k, True)), 'Check ALL rows matching the current filter, including rows not visible on this page.').pack(side='left', padx=3)
        self._help(ttk.Button(toolbar, text='Uncheck matching', command=lambda k=key: self._set_all(k, False)), 'Uncheck ALL results matching the active filter.').pack(side='left', padx=3)
        self._help(ttk.Button(toolbar, text='Toggle selected', command=lambda k=key: self._toggle(k)), 'Invert checkmarks only on highlighted rows in this tab. Double-clicking a row also toggles it.').pack(side='left', padx=3)
        self._help(ttk.Label(toolbar, text='  Filter:'), 'Filter table by relative path, problem type, or notes; matching operations apply to all filtered results.').pack(side='left', padx=(8, 2))
        filtervar = tk.StringVar()
        self.filters[key] = filtervar
        search = self._help(ttk.Entry(toolbar, textvariable=filtervar, width=20), 'Enter a filename, folder, status or note, then press Enter or Filter. Only display changes; file protection remains unchanged.')
        search.pack(side='left', padx=(0, 2))
        search.bind('<Return>', lambda e, k=key: self._filter(k))
        self._help(ttk.Button(toolbar, text='Filter', command=lambda k=key: self._filter(k)), 'Apply the typed search filter across all files in this tab.').pack(side='left', padx=3)
        self._help(ttk.Button(toolbar, text='‹', width=3, command=lambda k=key: self._go_page(k, -1)), 'Previous page of up to 2,000 entries.').pack(side='left', padx=(10, 1))
        pagelabel = ttk.Label(toolbar, text='0 / 0')
        pagelabel.pack(side='left')
        self.page_labels[key] = pagelabel
        self._help(ttk.Button(toolbar, text='›', width=3, command=lambda k=key: self._go_page(k, 1)), 'Next page of up to 2,000 entries.').pack(side='left', padx=1)
        container = ttk.Frame(frame)
        container.pack(fill='both', expand=True)
        cols = ('checked', 'kind', 'size', 'path', 'detail')
        tree = self._help(ttk.Treeview(container, columns=cols, show='headings', selectmode='extended'),
                          'Each row is a file or PAR group. Click to highlight; double-click or Space to check/uncheck. NEW files default checked; modifications/deletions need your approval.')
        for c, heading, width, stretch in [
            ('checked', '✓', 35, False), ('kind', 'Status', 122, False),
            ('size', 'File size', 95, False), ('path', 'Relative path', 510, True),
            ('detail', 'Notes', 430, True)]:
            tree.heading(c, text=heading)
            tree.column(c, width=width, minwidth=25, stretch=stretch,
                        anchor='center' if c == 'checked' else 'w')
        vs = ttk.Scrollbar(container, orient='vertical', command=tree.yview)
        hs = ttk.Scrollbar(container, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vs.grid(row=0, column=1, sticky='ns')
        hs.grid(row=1, column=0, sticky='ew')
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        tree.bind('<Double-1>', lambda e, k=key: self._toggle(k))
        tree.bind('<space>', lambda e, k=key: self._toggle(k))
        self.trees[key] = tree

    def _choose_dir(self, var):
        initial = var.get() if Path(var.get()).is_dir() else str(Path.home())
        result = filedialog.askdirectory(parent=self, initialdir=initial)
        if result:
            var.set(result)
            self._save()
            self._refresh_stats()

    def _choose_exe(self):
        path = filedialog.askopenfilename(title='Select MultiPar par2j64.exe or par2j.exe',
                                         filetypes=[('MultiPar CLI', 'par2j*.exe'), ('All files', '*.*')])
        if path:
            self.exe_var.set(path)
            self._save()

    def _opts(self) -> dict:
        vals = [int(v.get()) for v in (self.small_pct, self.large_pct, self.small_mb,
                                       self.group_mb, self.max_files)]
        sp, lp, cutoff, group, maximum = vals
        if not (1 <= sp <= 100 and 1 <= lp <= 100):
            raise ValueError('PAR redundancy % must be 1–100')
        if not (1 <= cutoff <= 1024 and 16 <= group <= 4096 and 1 <= maximum <= 2048):
            raise ValueError('Small cutoff 1–1024 MiB, group 16–4096 MiB, files/group 1–2048')
        if cutoff > group:
            raise ValueError('Small file cutoff must not exceed group size')
        return dict(small_pct=sp, large_pct=lp, small_mb=cutoff,
                    group_mb=group, max_files=maximum,
                    small_threads=self._live_thread_count('small'),
                    large_threads=self._live_thread_count('large'))

    def _save(self):
        try:
            APPDIR.mkdir(parents=True, exist_ok=True)
            options = self._opts()
            SETTINGS.write_text(json.dumps({'root': self.root_var.get(),
                                           'mirror': self.mirror_var.get(),
                                           'exe': self.exe_var.get(), 'rates': self.rates,
                                           **options}, indent=2), 'utf-8')
        except (ValueError, OSError):
            pass

    def _folder(self) -> Path | None:
        p = Path(self.root_var.get().strip())
        if not self.root_var.get().strip() or not p.is_dir():
            messagebox.showerror('Backup folder', 'Please select an existing backup folder / disk.')
            return None
        return p.resolve()

    def _exe(self) -> str | None:
        p = Path(self.exe_var.get().strip())
        if not p.is_file():
            messagebox.showerror('Missing MultiPar', 'Install MultiPar, then select par2j64.exe (not MultiPar.exe).')
            return None
        return str(p)

    def _start(self, title: str, action, *args):
        if self.busy:
            return
        self.busy = True
        self.current_task = title
        self.latest_progress = None
        self.cancel.clear()
        self.status.set(title)
        self.eta_label.set(('Metadata scan: reporting counted files; total count is not known in advance.'
                            if title == 'Quick scan' else
                            'Inventory comparison: reads only saved database records, not archive bytes.'
                            if title == 'Compare mirror' else
                            'Calculating work and measuring actual processing speed…'))
        self.progress.configure(mode='indeterminate', value=0)
        self.progress.start(12)
        self.stop_btn.configure(state='normal')
        for b in self.buttons:
            b.configure(state='disabled')
        self._save()
        self._log(f'Thread limits: small groups {self.live_threads["small"]}; large files {self.live_threads["large"]} (MultiPar /lcN).')

        def worker():
            try:
                result = action(*args, self.cancel, self._emit)
                self.events.put(('complete', (title, result)))
            except engine.Cancelled as exc:
                self.events.put(('failed', (str(exc), False)))
            except Exception as exc:
                self.events.put(('log', traceback.format_exc()))
                self.events.put(('failed', (str(exc), True)))
        threading.Thread(target=worker, daemon=True).start()

    def _emit(self, kind, data):
        self.events.put((kind, data))

    def _poll(self):
        try:
            for _ in range(300):
                kind, value = self.events.get_nowait()
                if kind == 'status':
                    self.status.set(value)
                elif kind == 'progress':
                    self.latest_progress = value
                    if str(self.progress.cget('mode')) != 'determinate':
                        self.progress.stop()
                        self.progress.configure(mode='determinate', maximum=100)
                    self.progress.configure(value=value['percent'])
                    self.status.set(value['stage'] or value['label'])
                    eta = progresslib.duration(value['eta_seconds'])
                    self.eta_label.set(f'Estimated remaining: {eta}  |  '
                                       f'Work: {engine.fmt_size(value["done"])} / {engine.fmt_size(value["total"])}  |  '
                                       f'Effective rate: {engine.fmt_size(int(value["speed"]))}/s'
                                       '  (PAR2 work approximate)')
                elif kind == 'log':
                    self._log(value)
                elif kind == 'issue':
                    self.issues.append(value)
                    self._append_item('issues', value, len(self.issues) - 1)
                elif kind == 'complete':
                    self._learn_rate()
                    self._finished(*value)
                elif kind == 'failed':
                    self._learn_rate()
                    self._idle()
                    self._log('STOPPED / ERROR: ' + value[0])
                    self.status.set(value[0])
                    if value[1]:
                        messagebox.showerror('Operation could not complete', value[0])
                    self._refresh_stats()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _log(self, message):
        self.log.configure(state='normal')
        self.log.insert('end', str(message) + '\n')
        if int(self.log.index('end-1c').split('.')[0]) > 2500:
            self.log.delete('1.0', '150.0')
        self.log.see('end')
        self.log.configure(state='disabled')
        if self.logger.handlers:
            self.logger.info('%s', str(message))

    def _idle(self):
        self.busy = False
        self.progress.stop()
        self.stop_btn.configure(state='disabled')
        self._save()
        for b in self.buttons:
            b.configure(state='normal')

    def _refresh_stats(self):
        if self.busy:
            return
        p = self.root_var.get().strip()
        if not p or not Path(p).is_dir():
            return
        try:
            root = Path(p)
            free = shutil.disk_usage(root).free
            if (root / engine.META / 'inventory.sqlite3').exists():
                with engine.db(root, initialize=False) as c:
                    count, total = c.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM files').fetchone()
                    sets = c.execute("SELECT COUNT(*) FROM sets WHERE state='active'").fetchone()[0]
                self.info.set(f'{count:,} protected files | {engine.fmt_size(total)} indexed | '
                              f'{sets:,} PAR sets | {engine.fmt_size(free)} disk free')
            else:
                self.info.set(f'No inventory yet. {engine.fmt_size(free)} available. '
                              'Run Scan and choose to protect NEW files.')
        except Exception as exc:
            self.info.set(f'Inventory status unavailable: {exc}')

    def _filter(self, key):
        items = self._get_items(key)
        search = self.filters[key].get().strip().casefold()
        if search:
            self.views[key] = [i for i, it in enumerate(items)
                               if search in it.path.casefold() or search in it.kind.casefold()
                               or search in it.detail.casefold()]
        else:
            self.views[key] = None
        self.pages[key] = 0
        self._render_page(key)

    def _view(self, key):
        view = self.views.get(key)
        return view if view is not None else range(len(self._get_items(key)))

    def _go_page(self, key, delta):
        n = len(self._view(key))
        maxpage = max(0, (n - 1) // self.page_size)
        self.pages[key] = max(0, min(maxpage, self.pages.get(key, 0) + delta))
        self._render_page(key)

    def _display(self, key: str, items: list[engine.Item]):
        self.views[key] = None
        self.pages[key] = 0
        self.filters[key].set('')
        self._render_page(key)

    def _render_page(self, key):
        items = self._get_items(key)
        tree = self.trees[key]
        existing = tree.get_children()
        if existing:
            tree.delete(*existing)
        view = self._view(key)
        n = len(view)
        page = self.pages.get(key, 0)
        start = page * self.page_size
        stop = min(n, start + self.page_size)
        for pos in range(start, stop):
            ix = view[pos]
            self._insert_row(key, items[ix], ix)
        self.page_labels[key].configure(text=f'{page+1} / {max(1,(n-1)//self.page_size+1)} ({n:,})')

    def _insert_row(self, key, item, ix):
        self.trees[key].insert('', 'end', iid=str(ix), values=(
            '☑' if item.selected else '☐', item.kind, engine.fmt_size(item.size),
            item.path, item.detail))

    def _append_item(self, key, item, ix):
        # Called while CRC scan is running: display only if present on current page.
        if self.views.get(key) is not None:
            view = self._view(key)
            if ix not in view[self.pages.get(key, 0) * self.page_size:
                              (self.pages.get(key, 0) + 1) * self.page_size]:
                return
        elif not (self.pages.get(key, 0) * self.page_size <= ix <
                  (self.pages.get(key, 0) + 1) * self.page_size):
            return
        self._insert_row(key, item, ix)
        if ix % 20 == 0:
            self.page_labels[key].configure(text=f'{self.pages.get(key,0)+1} / … ({len(self._get_items(key)):,})')

    def _get_items(self, key):
        return {'changes': self.changes, 'issues': self.issues,
                'comparison': self.comparison}[key]

    def _toggle(self, key):
        tree = self.trees[key]
        items = self._get_items(key)
        for id_ in tree.selection():
            i = int(id_)
            if i >= len(items):
                continue
            items[i].selected = not items[i].selected
            tree.set(id_, 'checked', '☑' if items[i].selected else '☐')
        return 'break'

    def _set_all(self, key, value):
        tree = self.trees[key]
        items = self._get_items(key)
        for i in self._view(key):
            items[i].selected = bool(value)
        for iid in tree.get_children():
            i = int(iid)
            tree.set(iid, 'checked', '☑' if items[i].selected else '☐')

    def _finished(self, title, result):
        self._idle()
        if title == 'Self-test MultiPar':
            self._log('Self-test PASSED. You can begin protecting real data.')
            self.status.set('MultiPar compatibility self-test PASSED')
            messagebox.showinfo('MultiPar test passed', 'MultiPar successfully created, verified, repaired and re-checked a temporary set, including Unicode filenames.', parent=self)
        elif title == 'Quick scan':
            self.changes = result
            self._display('changes', self.changes)
            self.notebook.select(0)
            stats = {k: sum(x.kind == k for x in result) for k in ('NEW', 'CHANGED', 'MISSING')}
            self._log(f'CHANGES: {stats["NEW"]} new, {stats["CHANGED"]} changed, {stats["MISSING"]} missing')
            self.status.set('Quick scan complete. NEW checked; CHANGED/MISSING unchecked until approved.')
            self._refresh_stats()
            approved_deleted = False
            if stats['MISSING']:
                approved_deleted = messagebox.askyesno(
                    'Files missing / deleted',
                    f'{stats["MISSING"]:,} protected files are missing.\n\n'
                    'Were ALL of these files intentionally deleted?\n\n'
                    'YES: approve their removal from inventory and rebuild affected PAR sets.\n'
                    'NO: keep their original checksums and PAR recovery protection.\n\n'
                    'If unsure, choose NO and run CRC check / repair.', parent=self)
                if approved_deleted:
                    for i, x in enumerate(self.changes):
                        if x.kind == 'MISSING':
                            x.selected = True
                            if self.trees['changes'].exists(str(i)):
                                self.trees['changes'].set(str(i), 'checked', '☑')
            if stats['NEW'] or approved_deleted:
                count = stats['NEW'] + (stats['MISSING'] if approved_deleted else 0)
                if messagebox.askyesno('Apply new/deleted files?',
                                       f'{count:,} approved changes can now be applied.\n\n'
                                       'This calculates new SHA256 + CRC32 and builds/verifies PAR2 archives. '
                                       'Live remaining-time estimates will update from measured processing speed.\n\n'
                                       'Proceed now?  (Modified files require separate approval.)',
                                       parent=self):
                    self.apply()
            if stats['CHANGED']:
                self._log('IMPORTANT: Changed files are UNCHECKED. Review each before accepting new baseline!')
        elif title == 'Apply changes':
            self._log(f'Changes applied: {result}')
            self.status.set('Changes applied. Run Scan again to refresh the list.')
            self._refresh_stats()
            messagebox.showinfo('Completed', f'Committed {result.get("done",0)} PAR groups.\n'
                                f'New/updated: {result.get("files",0)}. '
                                f'Deleted: {result.get("deleted",0)}.\n\n'
                                'Run Scan for changes to refresh.', parent=self)
        elif title == 'Full checksum check':
            self.issues = result
            self._display('issues', result)
            self.notebook.select(1)
            self.status.set(f'Full checksum check complete — {len(result):,} issues')
            if not result:
                messagebox.showinfo('Checksums OK', 'All tracked file contents match their saved SHA256 and CRC32.\n'
                                    'PAR2 recovery volumes were checked for presence, not fully verified.\n'
                                    'Use Verify PAR sets to verify parity data.', parent=self)
        elif title == 'Repair checked files':
            self.issues = result
            self._display('issues', result)
            self.status.set(f'Repair completed. Remaining reported issues: {len(result)}')
            self._refresh_stats()
            messagebox.showinfo('Repair finished', 'Review the activity log and remaining issues. '
                                'Run a full CRC/SHA256 check again to confirm the entire archive.', parent=self)
        elif title == 'Verify recovery sets':
            # Parity scan doesn't erase earlier checksum results.
            self.issues = [x for x in self.issues if not x.kind.startswith('PAR_')] + result
            self._display('issues', self.issues)
            self.notebook.select(1)
            self.status.set(f'PAR verification finished: {len(result)} failing sets')
        elif title == 'Rebuild PAR sets':
            self._log(f'PAR sets rebuilt: {result.get("done", 0)}')
            self.status.set('PAR rebuild complete. Run Verify PAR sets again.')
            self._refresh_stats()
        elif title == 'Compare mirror':
            self.comparison = result
            self._display('comparison', result)
            self.notebook.select(2)
            self.status.set(f'Mirror inventories compared: {len(result)} differences')

    def test_par(self):
        exe = self._exe()
        if exe:
            self._start('Self-test MultiPar', engine.selftest, exe)

    def scan(self):
        root = self._folder()
        if root:
            self._start('Quick scan', engine.scan_changes, root)

    def apply(self):
        root = self._folder()
        exe = self._exe()
        if not root or not exe:
            return
        try:
            opts = self._opts()
        except ValueError as e:
            messagebox.showerror('Recovery settings', str(e))
            return
        selected = [x for x in self.changes if x.selected]
        if not selected:
            messagebox.showinfo('Nothing selected', 'Tick NEW, CHANGED or MISSING files in the Changes tab first.')
            return
        n_new = sum(x.kind == 'NEW' for x in selected)
        n_edit = sum(x.kind == 'CHANGED' for x in selected)
        n_del = sum(x.kind == 'MISSING' for x in selected)
        new_bytes = sum(x.size for x in selected if x.kind == 'NEW')
        rough_par = sum(x.size * (opts['small_pct'] if x.size < opts['small_mb'] * engine.MIB
                                   else opts['large_pct']) / 100
                        for x in selected if x.kind == 'NEW')
        free = shutil.disk_usage(root).free
        estimate_msg = (f'New data: {engine.fmt_size(new_bytes)}. '
                        f'Estimated NEW PAR2 overhead: ~{engine.fmt_size(int(rough_par))}. '
                        f'Available on this disk: {engine.fmt_size(free)}.\n\n'
                        'Actual overhead may exceed the estimate, especially for small groups.\n'
                        + self._estimate_text(3 * (new_bytes + sum(x.size for x in selected if x.kind == 'CHANGED')),
                                              'Apply changes'))
        if rough_par * 1.2 > free:
            if not messagebox.askyesno('WARNING — insufficient estimated space',
                                       estimate_msg + '\n\nExpected PAR2 data may not fit. '
                                       'Reduce the recovery percentages or free disk space. '
                                       'Continue anyway?', parent=self):
                return
        if n_edit or n_del:
            if not messagebox.askyesno('Critical: approve baseline update',
                                       f'New files: {n_new}\nModified file contents: {n_edit}\n'
                                       f'Intentionally deleted: {n_del}\n\n'
                                       'Approving a missing file removes its INVENTORY entry only; '                                       'it does NOT delete disk files (already missing). '                                       'An accepted edit/deletion REPLACES prior PAR groups and checksum baselines. '
                                       'Do NOT approve suspicious modifications or missing files caused by disk problems.\n\n' +
                                       estimate_msg + '\n\nHave you confirmed these changes are intentional?', parent=self):
                return
        elif not messagebox.askyesno('Create PAR2 recovery files',
                                     f'Protect {n_new:,} new files now? This can read and write a lot of data.\n\n' + estimate_msg,
                                     parent=self):
            return
        # Copy selections; don't mutate the list during the worker operation.
        payload = [engine.dataclasses.replace(x) for x in self.changes]
        self._start('Apply changes', engine.apply_changes, root, payload, self._thread_options(), exe)

    def check(self):
        root = self._folder()
        if not root:
            return
        if not (root / engine.META / 'inventory.sqlite3').exists():
            messagebox.showinfo('No inventory', 'Run Scan and protect files before CRC check.')
            return
        with engine.db(root, initialize=False) as conn:
            count, total_bytes = conn.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM files').fetchone()
        if not messagebox.askyesno('Full disk integrity scan',
                                   f'This reads {engine.fmt_size(total_bytes)} across {count:,} tracked files '
                                   '(SHA256 + CRC32).\n'
                                   f'{self._estimate_text(total_bytes, "Full checksum check")}\n\nProceed?', parent=self):
            return
        self.issues = []
        self._display('issues', [])
        self.notebook.select(1)
        self._start('Full checksum check', engine.crc_check, root)

    def repair(self):
        root = self._folder()
        exe = self._exe()
        if not root or not exe:
            return
        affected = [x for x in self.issues if x.selected and x.kind in ('CORRUPT', 'MISSING', 'UNREADABLE')]
        if not affected:
            messagebox.showinfo('No files selected', 'Run CRC check, then tick files to restore.')
            return
        selected_groups = {x.group for x in affected}
        with engine.db(root, initialize=False) as conn:
            group_sizes = {row['id']: row['total_bytes'] for row in
                           conn.execute("SELECT id,total_bytes FROM sets WHERE state='active'")}
        planned_work = 3 * sum(group_sizes.get(g, 0) for g in selected_groups)
        if not messagebox.askyesno('Repair selected files',
                                   f'Repair {len(affected)} selected files from PAR2?\n'
                                   f'Affected recovery groups: {len(selected_groups)}. '
                                   f'Estimated I/O and hashing work: {engine.fmt_size(planned_work)}.\n'
                                   f'{self._estimate_text(planned_work, "Repair checked files")}\n\n'
                                   'MultiPar repairs entire recovery GROUPS, not individual files. '
                                   'All damaged files in a chosen group must be selected.\n'
                                   'Any modified files are restored to the saved baseline.\n'
                                   'MultiPar uses /b and keeps damaged originals in .paritykeeper/quarantine.\n\n'
                                   'Run repair now?', parent=self):
            return
        payload = [engine.dataclasses.replace(x) for x in self.issues]
        self._start('Repair checked files', functools.partial(engine.repair_selected, threads=self._threads_only()), root, payload, exe)

    def verify_par(self):
        root = self._folder()
        exe = self._exe()
        if not root or not exe:
            return
        with engine.db(root, initialize=False) as conn:
            count, total = conn.execute("SELECT COUNT(*),COALESCE(SUM(total_bytes),0) FROM sets WHERE state='active'").fetchone()
        if not messagebox.askyesno('Verify PAR files',
                                   f'Verify {count:,} PAR2 recovery sets with source data totalling '
                                   f'{engine.fmt_size(total)}?\n'
                                   f'{self._estimate_text(total, "Verify recovery sets")}\n\nProceed?', parent=self):
            return
        self._start('Verify recovery sets', functools.partial(engine.verify_all_par, threads=self._threads_only()), root, exe)

    def rebuild_par(self):
        root = self._folder()
        exe = self._exe()
        if not root or not exe:
            return
        try:
            opts = self._opts()
        except ValueError as e:
            messagebox.showerror('Recovery settings', str(e))
            return
        selected = [x for x in self.issues if x.selected and x.kind.startswith('PAR_')]
        if not selected:
            messagebox.showinfo('No recovery sets selected',
                                'Run CRC check or Verify PAR sets, then tick PAR_MISSING or PAR_VERIFY entries.')
            return
        if not messagebox.askyesno('Rebuild missing or damaged recovery sets',
                                   f'Rebuild {len(selected)} PAR2 recovery sets?\n\n'
                                   'All source files MUST match the original saved SHA256. '
                                   'Otherwise the operation will refuse to proceed. '
                                   'Old recovery files are replaced only after new recovery files verify.',
                                   parent=self):
            return
        self._start('Rebuild PAR sets', engine.rebuild_par, root,
                    [engine.dataclasses.replace(x) for x in self.issues], self._thread_options(), exe)

    def compare(self):
        root = self._folder()
        other = Path(self.mirror_var.get().strip())
        if not root:
            return
        if not self.mirror_var.get().strip() or not other.is_dir():
            messagebox.showerror('Mirror folder', 'Select a valid mirror folder.')
            return
        for label, folder in [('primary', root), ('mirror', other)]:
            if not (folder / engine.META / 'inventory.sqlite3').is_file():
                messagebox.showerror('Inventory missing',
                                     f'No {label} inventory found in {folder}. Scan/protect it first.')
                return
        self._start('Compare mirror', engine.compare_inventories, root, other.resolve())

    def review_orphans(self):
        if self.busy:
            return
        root = self._folder()
        if root is None:
            return
        try:
            dirs = engine.list_orphan_par_dirs(root)
        except (OSError, engine.SafetyError) as exc:
            messagebox.showerror('Review leftover PAR2', str(exc), parent=self)
            return
        if not dirs:
            messagebox.showinfo('Leftover recovery data',
                                'No untracked PAR2 directories were found. No files were changed.', parent=self)
            return
        text = '\n'.join(str(p) for p in dirs[:18])
        messagebox.showwarning('Untracked PAR2 - review manually',
                               f'{len(dirs)} directory(s) are not referenced in the inventory.\n\n'
                               f'{text}\n\n'
                               'Nothing has been deleted. These may be useful after a crash; '
                               'inspect carefully before any manual cleanup.', parent=self)
        self._log(f'Found {len(dirs)} untracked PAR2 folders (no automatic deletion).')

    def export_csv(self):
        key = ('changes', 'issues', 'comparison')[self.notebook.index('current')]
        items = self._get_items(key)
        if not items:
            messagebox.showinfo('Empty list', 'No entries to export in this tab.')
            return
        path = filedialog.asksaveasfilename(title='Save CSV report', defaultextension='.csv',
                                            filetypes=[('CSV report', '*.csv')])
        if path:
            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                w.writerow(['Selected', 'Status', 'Relative path', 'Size bytes', 'PAR group', 'Notes'])
                for x in items:
                    w.writerow([x.selected, x.kind, x.path, x.size, x.group, x.detail])
            self._log(f'CSV exported: {path}')

    def _close(self):
        if self.busy:
            messagebox.showwarning('Operation in progress', 'Press STOP and allow the current operation to end before closing.')
            return
        self._save()
        self.destroy()


if __name__ == '__main__':
    try:
        app = Window()
        app.mainloop()
    except Exception:
        traceback.print_exc()
        raise
