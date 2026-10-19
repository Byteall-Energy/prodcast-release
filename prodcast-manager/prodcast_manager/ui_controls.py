"""Shared branding and non-value example hints for desktop controls."""
import sys
from pathlib import Path
import tkinter as tk
from tkinter import ttk


def apply_branding(window, default=False):
    resources = Path(__file__).parent / 'resources'
    if default and sys.platform == 'win32':
        import ctypes
        try: ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('ProdCast.Manager')
        except (AttributeError, OSError): pass
    # Tk's default icon applies to future Toplevels, including nested dialogs.
    try:
        if default:
            photo = tk.PhotoImage(master=window, file=str(resources / 'prodcast-manager.png'))
            window.iconphoto(True, photo)
            window._prodcast_icon = photo
        if sys.platform == 'win32':
            if default: window.iconbitmap(default=str(resources / 'prodcast-manager.ico'))
            window.iconbitmap(str(resources / 'prodcast-manager.ico'))
    except tk.TclError:
        pass  # An unavailable icon must not prevent access to deployment/recovery.
    if default:
        # Built-in simpledialog windows are constructed by Tk itself. Give them
        # an explicit icon when mapped instead of relying only on class fallback.
        def brand_popup(event):
            if isinstance(event.widget, tk.Toplevel): apply_branding(event.widget)
        window.bind_all('<Map>', brand_popup, add='+')


class ExampleEntry(ttk.Entry):
    """The hint is a separate label, never text in the entry/StringVar."""
    def __init__(self, parent, *, textvariable, example, **kwargs):
        super().__init__(parent, textvariable=textvariable, **kwargs)
        self._value = textvariable
        self.example = example
        self._hint = ttk.Label(self, text=example, foreground='#737b85', background='#ffffff')
        self._hint.bind('<Button-1>', lambda event: self.focus_set())
        self.bind('<FocusIn>', self._update_hint, add='+')
        self.bind('<FocusOut>', self._update_hint, add='+')
        self.bind('<Configure>', self._update_hint, add='+')
        self.bind('<Destroy>', self._destroy_hint, add='+')
        self._trace = textvariable.trace_add('write', self._update_hint)
        self._update_hint()

    def _update_hint(self, *_):
        if self._value.get() or self.focus_get() == self:
            self._hint.place_forget()
        else:
            self._hint.place(x=8, rely=.5, anchor='w')

    def _destroy_hint(self, event):
        if event.widget == self:
            self._value.trace_remove('write', self._trace)


def fresh_site():
    from .config import example
    value = example()
    value['install_ai'] = False
    for host in value['hosts'].values(): host['address'] = ''
    value['public_url'] = ''
    value['admin_ip'] = ''
    return value
