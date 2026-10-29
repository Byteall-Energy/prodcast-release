"""Real Tk workspace smoke and frozen dependency verification."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from prodcast_manager.tk_ui import smoke_test


def capture_preview(destination,scenario=None):
    """Capture only this test's own Tk HWND, never the desktop or other apps."""
    import ctypes as c
    import tempfile
    import tkinter as tk
    from ctypes import wintypes as w
    import gc
    import struct
    import zlib
    from prodcast_manager.tk_ui import DesktopWindow
    from prodcast_manager.desktop_controller import DesktopController
    class Info(c.Structure):
        _fields_=[('size',w.DWORD),('width',w.LONG),('height',w.LONG),('planes',w.WORD),
                  ('bits',w.WORD),('compression',w.DWORD),('image',w.DWORD),
                  ('x',w.LONG),('y',w.LONG),('used',w.DWORD),('important',w.DWORD)]
    u=c.windll.user32;g=c.windll.gdi32
    u.GetAncestor.argtypes=[w.HWND,w.UINT];u.GetAncestor.restype=w.HWND
    u.GetWindowRect.argtypes=[w.HWND,c.POINTER(w.RECT)]
    u.GetWindowDC.argtypes=[w.HWND];u.GetWindowDC.restype=w.HDC
    u.PrintWindow.argtypes=[w.HWND,w.HDC,w.UINT]
    u.ReleaseDC.argtypes=[w.HWND,w.HDC]
    g.CreateCompatibleDC.argtypes=[w.HDC];g.CreateCompatibleDC.restype=w.HDC
    g.CreateCompatibleBitmap.argtypes=[w.HDC,c.c_int,c.c_int];g.CreateCompatibleBitmap.restype=w.HBITMAP
    g.SelectObject.argtypes=[w.HDC,w.HGDIOBJ];g.SelectObject.restype=w.HGDIOBJ
    g.DeleteObject.argtypes=[w.HGDIOBJ];g.DeleteDC.argtypes=[w.HDC]
    g.GetDIBits.argtypes=[w.HDC,w.HBITMAP,w.UINT,w.UINT,c.c_void_p,c.c_void_p,w.UINT]
    with tempfile.TemporaryDirectory(prefix='manager-tk-preview-') as folder:
        gc.collect()
        controller=DesktopController(Path(folder))
        if scenario:
            from prodcast_manager.config import atomic_json
            controller.language_set({'language':'en'})
            if scenario=='attention':
                atomic_json(controller.directory/'maintenance-journal.json',{'mode':'export','status':'failed','operation':'preview-backup','services_recovered':True,'leases_released':True})
            elif scenario=='blocking':
                atomic_json(controller.directory/'maintenance-journal.json',{'mode':'export','status':'failed','operation':'preview-backup','quiesced':True,'services_recovered':False,'leases_released':True})
            else:raise ValueError('Unknown preview scenario')
        root=tk.Tk();window=DesktopWindow(root,controller);window.navigate('overview' if scenario else 'servers')
        root.update();hwnd=u.GetAncestor(root.winfo_id(),2);rect=w.RECT();u.GetWindowRect(hwnd,c.byref(rect))
        width,height=rect.right-rect.left,rect.bottom-rect.top
        dc=u.GetWindowDC(hwnd);memory=g.CreateCompatibleDC(dc);bitmap=g.CreateCompatibleBitmap(dc,width,height)
        old=g.SelectObject(memory,bitmap)
        try:
            if not u.PrintWindow(hwnd,memory,2):raise RuntimeError('Could not capture Manager test window')
            g.SelectObject(memory,old);info=Info(c.sizeof(Info),width,-height,1,32,0,0,0,0,0,0)
            pixels=c.create_string_buffer(width*height*4)
            if not g.GetDIBits(memory,bitmap,0,height,pixels,c.byref(info),0):raise RuntimeError('No Manager image pixels')
            path=Path(destination);path.parent.mkdir(parents=True,exist_ok=True)
            rgb=bytearray(width*height*3)
            rgb[0::3]=pixels.raw[2::4];rgb[1::3]=pixels.raw[1::4];rgb[2::3]=pixels.raw[0::4]
            def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
            rows=b''.join(b'\0'+rgb[y*width*3:(y+1)*width*3] for y in range(height))
            path.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b''))
        finally:
            g.SelectObject(memory,old);g.DeleteObject(bitmap);g.DeleteDC(memory);u.ReleaseDC(hwnd,dc);window.destroy()


def verify_archive(names):
    forbidden=('pyside','shiboken','qtwebengine','qt6core','qt6widgets','qt6qml','qt6quick','/webui/')
    bad=[name for name in names if any(part in name.replace('\\','/').lower() for part in forbidden)]
    if bad:raise RuntimeError('Unexpected Qt/browser dependency: '+', '.join(bad[:10]))
    if not any('_tkinter' in name.lower() for name in names):raise RuntimeError('Tk runtime missing')


if __name__=='__main__':
    smoke_test()
    if '--capture' in sys.argv:capture_preview(sys.argv[sys.argv.index('--capture')+1],sys.argv[sys.argv.index('--scenario')+1] if '--scenario' in sys.argv else None)
    if '--verify-exe' in sys.argv:
        from PyInstaller.archive.readers import CArchiveReader
        verify_archive(CArchiveReader(sys.argv[sys.argv.index('--verify-exe')+1]).toc)
    print('PASS: styled Tkinter, seven preview sections, server table, visible worker actions, optional LDAP search account; no Qt/browser runtime')
