"""Preview-style desktop workspace implemented entirely in Tkinter/ttk."""
import copy
import json
import os
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog, simpledialog

from . import __version__
from .desktop_controller import DesktopController
from .portable import LegacyPasswordRequired
from .ui_controls import ExampleEntry, apply_branding

BG='#f4f6fa'
INK='#203044'
MUTED='#65758b'
BLUE='#2165c4'


class Form(ttk.LabelFrame):
    """A card with labelled, expanding rows."""
    def __init__(self,parent,title):
        super().__init__(parent,text=title,padding=16,style='Card.TLabelframe')
        self.pack(fill='x',pady=(0,15));self.columnconfigure(1,weight=1);self.row=0

    def addRow(self,*items):
        if len(items)==1:
            items[0].grid(in_=self,row=self.row,column=0,columnspan=2,sticky='ew',pady=5)
        else:
            label,value=items
            if isinstance(label,str):label=ttk.Label(self,text=label,style='Card.TLabel',wraplength=225)
            label.grid(in_=self,row=self.row,column=0,sticky='nw',padx=(0,14),pady=8)
            value.grid(in_=self,row=self.row,column=1,sticky='ew',pady=5)
        self.row+=1


class Entry(ExampleEntry):
    def clear(self):self._value.set('')


class DesktopWindow:
    sections=(('overview','Overview','Обзор'),('servers','Servers & workers','Серверы и Workers'),
        ('deploy','Install / update','Установка / обновление'),('ai','AI','AI'),
        ('settings','App settings','Настройки App'),('backups','Backups & recovery','Бэкапы и восстановление'),
        ('operations','Operations','Операции'))

    def __init__(self,root,controller):
        self.root=root;self.controller=controller;self.state=controller.snapshot()
        self.config=copy.deepcopy(self.state['config']);self.packages=dict(self.state['packages'])
        self.membership_stamp=self.state.get('membership_stamp')
        self.page='overview';self.role='app';self.setting='general';self.step=0
        self.deploy={'mode':'update' if self.state['installed'] else 'install'}
        self.access={};self.sudo_modes={};self.worker_services=copy.deepcopy(self.state.get('worker_services',{}));self.directory=dict(self.state['directory']);self.activation=dict(self.state['activation'])
        self.certificate={};self.maintenance={};self.result_seen=None;self.fields={};self.mutable=[]
        self.operation='';self.local_dirty=False;self.after_id=None;self.closed=False;self.traces=[]
        root.title('ProdCast Manager '+__version__);root.geometry('1180x850');root.minsize(980,680)
        root.configure(background=BG);apply_branding(root,default=True)
        self.style=ttk.Style(root);self.style.theme_use('clam')
        self.style.configure('.',font=('Segoe UI',10),foreground=INK,background=BG)
        self.style.configure('TButton',padding=(12,8),background='white',bordercolor='#cbd5e3')
        self.style.map('TButton',background=[('active','#e8f0fc')],foreground=[('disabled','#8993a2')])
        self.style.configure('Primary.TButton',background=BLUE,foreground='white',bordercolor=BLUE)
        self.style.map('Primary.TButton',background=[('active','#1553a2'),('disabled','#d3dae5')])
        self.style.configure('Nav.TButton',anchor='w',padding=(16,13),background='#edf1f7',borderwidth=0)
        self.style.configure('Selected.Nav.TButton',background='#dce9fc',foreground='#1553a2')
        self.style.configure('Side.TFrame',background='#edf1f7')
        self.style.configure('White.TFrame',background='white')
        self.style.configure('Card.TLabelframe',background='white',bordercolor='#dde3ed',relief='solid')
        self.style.configure('Card.TLabelframe.Label',background=BG,font=('Segoe UI',10,'bold'))
        self.style.configure('Card.TLabel',background='white')
        self.style.configure('TEntry',fieldbackground='white',padding=7,bordercolor='#cbd5e3')
        self.style.configure('TCombobox',fieldbackground='white',padding=7)
        self.style.configure('Treeview',rowheight=34,background='white',fieldbackground='white',bordercolor='#dde3ed')
        self.style.configure('Treeview.Heading',background='#edf1f7',padding=8,font=('Segoe UI',10,'bold'))
        self.style.map('Treeview',background=[('selected','#dfebff')],foreground=[('selected','#184d96')])
        header=ttk.Frame(root,padding=(22,14),style='White.TFrame');header.pack(fill='x')
        self.brand=tk.PhotoImage(master=root,file=str(Path(__file__).parent/'resources/prodcast-manager.png'))
        ratio=max(1,self.brand.width()//28);self.brand_small=self.brand.subsample(ratio)
        ttk.Label(header,image=self.brand_small,background='white').pack(side='left',padx=(0,8))
        ttk.Label(header,text='ProdCast Manager',font=('Segoe UI',16,'bold'),background='white').pack(side='left')
        ttk.Label(header,text='  '+__version__,background='white',foreground=MUTED).pack(side='left')
        self.admin=ttk.Button(header,command=lambda:self.guard(self.administrator));self.admin.pack(side='right')
        self.lang=tk.StringVar(value='Русский' if controller.language=='ru' else 'English')
        self.language=ttk.Combobox(header,textvariable=self.lang,values=('English','Русский'),state='readonly',width=9)
        self.language.pack(side='right',padx=10);self.language.bind('<<ComboboxSelected>>',lambda e:self.guard(self.change_language))
        self.no_wheel(self.language)
        context=ttk.Frame(root,padding=(22,10));context.pack(fill='x')
        self.site=tk.StringVar();self.site_picker=ttk.Combobox(context,textvariable=self.site,state='readonly',width=28)
        self.site_picker.pack(side='left',padx=(0,8));self.no_wheel(self.site_picker)
        self.site_picker.bind('<<ComboboxSelected>>',lambda e:self.guard(lambda:self.change_site('select_site',{'id':self.site.get()})))
        self.site_buttons=[]
        for action,en,ru in [('new_site','New site','Новая площадка'),('import_profile','Open profile…','Открыть профиль…'),('save_draft','Save draft','Сохранить черновик')]:
            b=ttk.Button(context,text=self.t(en,ru),command=lambda a=action:self.guard(lambda:self.site_action(a)))
            b.pack(side='left',padx=3);self.site_buttons.append((b,en,ru))
        self.status=tk.StringVar();ttk.Label(context,textvariable=self.status,foreground=MUTED).pack(side='right')
        body=ttk.Frame(root);body.pack(fill='both',expand=True)
        side=ttk.Frame(body,width=225,padding=(8,18),style='Side.TFrame');side.pack(side='left',fill='y');side.pack_propagate(False)
        self.nav={}
        for key,en,ru in self.sections:
            b=ttk.Button(side,text=self.t(en,ru),style='Nav.TButton',command=lambda p=key:self.guard(lambda:self.navigate(p)))
            b.pack(fill='x',pady=2);self.nav[key]=b
        area=ttk.Frame(body);area.pack(side='left',fill='both',expand=True)
        self.canvas=tk.Canvas(area,background=BG,highlightthickness=0)
        bar=ttk.Scrollbar(area,orient='vertical',command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set);bar.pack(side='right',fill='y');self.canvas.pack(fill='both',expand=True)
        self.content=ttk.Frame(self.canvas,padding=(28,22))
        self.canvas_window=self.canvas.create_window((0,0),window=self.content,anchor='nw')
        self.content.bind('<Configure>',lambda e:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',lambda e:self.canvas.itemconfigure(self.canvas_window,width=e.width))
        root.bind_all('<MouseWheel>',self.wheel,add='+')
        self.footer=tk.StringVar();ttk.Label(root,textvariable=self.footer,padding=(12,6),foreground=MUTED).pack(fill='x')
        self.render();self.refresh_context();root.protocol('WM_DELETE_WINDOW',self.close)
        self.after_id=root.after(500,lambda:self.guard(self.poll))

    def t(self,en,ru):return ru if self.controller.language=='ru' else en

    def guard(self,fn):
        try:return fn()
        except LegacyPasswordRequired:
            password=simpledialog.askstring('ProdCast Manager',self.t('Old vault password','Прежний пароль vault'),show='*',parent=self.root)
            if password is not None:
                try:self.controller.unlock_legacy({'password':password});return fn()
                except Exception as error:self.message('Credentials',self.controller.log.redact(str(error)),True)
        except Exception as error:
            self.controller.log.protect({'directory':self.directory,'activation':self.activation,'ssh':self.access,'worker_services':self.worker_services})
            self.message(self.t('Action needs attention','Требуется внимание'),self.controller.log.redact(str(error)),True)

    def message(self,title,text,error=False):
        (messagebox.showerror if error else messagebox.showinfo)('ProdCast Manager — '+title,str(text),parent=self.root)

    def confirm(self,title,text):
        return messagebox.askokcancel('ProdCast Manager — '+title,text,parent=self.root,default='cancel')

    def label(self,text,parent=None,muted=False):
        w=ttk.Label(parent or self.content,text=str(text),foreground=MUTED if muted else INK,wraplength=760)
        if parent is not None:w.pack(anchor='w',fill='x',pady=(0,10))
        return w

    def note(self,en,ru):
        w=self.label(self.t(en,ru),self.content);w.configure(background='#e9f1ff',padding=12);return w

    def group(self,en,ru):return Form(self.content,self.t(en,ru))

    def button(self,parent,en,ru,fn,primary=False,mutable=True):
        b=ttk.Button(parent,text=self.t(en,ru),command=lambda:self.guard(fn),style='Primary.TButton' if primary else 'TButton')
        if isinstance(parent,Form):parent.addRow(b)
        else:b.pack(anchor='w',pady=(0,8))
        if mutable:self.mutable.append((b,'normal'));b.configure(state='disabled' if self.state['busy'] else 'normal')
        return b

    def edit(self,form,en,ru,model,key,hint='',secret=False,browse=None,readonly=False):
        line=ttk.Frame(form,style='White.TFrame');var=tk.StringVar(master=self.root,value=str(model.get(key,'') or ''))
        entry=Entry(line,textvariable=var,example=hint,show='•' if secret else '')
        entry.pack(side='left',fill='x',expand=True);self.fields[key]=entry
        if readonly:entry.configure(state='readonly')
        else:
            token=var.trace_add('write',lambda *_:self.edited(model,key,var.get()))
            self.traces.append((var,token))
            self.mutable.append((entry,'normal'));entry.configure(state='disabled' if self.state['busy'] else 'normal')
        if secret:
            show=tk.BooleanVar(master=self.root,value=False)
            ttk.Checkbutton(line,text=self.t('Show','Показать'),variable=show,command=lambda:entry.configure(show='' if show.get() else '•')).pack(side='left',padx=6)
        if browse:
            b=ttk.Button(line,text=self.t('Browse…','Выбрать…'),command=lambda:self.guard(lambda:self.pick_into(var,model,key,browse)))
            b.pack(side='left',padx=(6,0));self.mutable.append((b,'normal'));b.configure(state='disabled' if self.state['busy'] else 'normal')
        if readonly:ttk.Button(line,text=self.t('Copy','Копировать'),command=lambda:self.copy(var.get())).pack(side='left',padx=6)
        form.addRow(self.t(en,ru),line);return entry

    def edited(self,model,key,value):
        model[key]=value;self.local_dirty=True
        if self.page=='servers' and hasattr(self,'server_tree') and model is self.config['hosts'].get(self.role) and key in ('address','username'):
            self.update_server_row(self.role)

    def check(self,form,en,ru,model,key):
        var=tk.BooleanVar(master=self.root,value=bool(model.get(key,False)))
        w=ttk.Checkbutton(form,text=self.t(en,ru),variable=var,command=lambda:self.edited(model,key,var.get()))
        w._value=var;form.addRow(w);self.fields[key]=w;self.mutable.append((w,'normal'))
        w.configure(state='disabled' if self.state['busy'] else 'normal');return w

    def combo(self,form,en,ru,model,key,choices,rerender=False):
        values=[value for value,text in choices];texts=[text for value,text in choices]
        current=model.get(key,values[0]);var=tk.StringVar(master=self.root,value=texts[values.index(current)] if current in values else texts[0])
        if key not in model:model[key]=values[0]
        w=ttk.Combobox(form,textvariable=var,values=texts,state='readonly')
        def change(e=None):
            self.edited(model,key,values[w.current()])
            if rerender:self.render()
        w.bind('<<ComboboxSelected>>',lambda e:self.guard(change));self.no_wheel(w)
        form.addRow(self.t(en,ru),w);self.fields[key]=w;w._value=var;self.mutable.append((w,'readonly'))
        w.configure(state='disabled' if self.state['busy'] else 'readonly');return w

    def no_wheel(self,widget):
        widget.bind('<MouseWheel>',self.wheel)
        widget.bind('<Button-4>',lambda e:self.wheel(e,-1))
        widget.bind('<Button-5>',lambda e:self.wheel(e,1))

    def wheel(self,event,direction=None):
        if not isinstance(event.widget,tk.Misc):return
        if event.widget.winfo_toplevel()!=self.root:return
        if isinstance(event.widget,(tk.Text,ttk.Treeview)):return
        self.canvas.yview_scroll(direction if direction is not None else (-1 if event.delta>0 else 1),'units')
        return 'break'

    def pick(self,kind):
        filters={'zip':[('ZIP','*.zip')],'models':[('AI models','*.zip *.gguf')],'key':[('SSH private key','*')],
            'ca':[('CA certificates','*.pem *.crt *.cer')],'pfx':[('PFX / P12','*.pfx *.p12')],
            'license':[('License','*.license')],'public-key':[('Public key','*.pem')],
            'profile':[('Site configuration','*.json')],'backup':[('Encrypted backup','*.pcbackup')],
            'backup-save':[('Encrypted backup','*.pcbackup')],'log':[('Session log','*.log')]}
        options=dict(parent=self.root,title='ProdCast Manager — '+self.t('Choose file','Выбор файла'),filetypes=filters[kind])
        if kind=='backup-save':return filedialog.asksaveasfilename(**options,defaultextension='.pcbackup')
        if kind=='log':options['initialdir']=str(self.controller.log.directory)
        return filedialog.askopenfilename(**options)

    def pick_into(self,var,model,key,kind):
        path=self.pick(kind)
        if path:var.set(path)

    def copy(self,value):self.root.clipboard_clear();self.root.clipboard_append(value)
    def toast(self,text,timeout=None):self.footer.set(text)

    def sync(self):
        for role,mode in self.sudo_modes.items():
            if mode['mode']=='root' and self.config['hosts'][role]['username']!='root':
                raise ValueError(self.t('Root privileges require SSH username root. Otherwise select a sudo mode.','Для root укажите SSH-пользователя root. Иначе выберите режим sudo.'))
        result=self.controller.update({'config':self.config,'packages':self.packages,'membership_stamp':self.membership_stamp})
        if result.get('membership_reconciled'):self.local_dirty=False;self.refresh()

    def refresh_context(self):
        self.site_picker.configure(values=[x['id'] for x in self.state['sites']]);self.site.set(self.state['site'])
        self.site_picker.configure(state='disabled' if self.state['busy'] else 'readonly')
        self.language.configure(state='disabled' if self.state['busy'] else 'readonly')
        self.admin.configure(text=self.t('App administrator access','Доступ администратора App'))
        for b,en,ru in self.site_buttons:b.configure(text=self.t(en,ru),state='disabled' if self.state['busy'] else 'normal')
        for key,en,ru in self.sections:self.nav[key].configure(text=self.t(en,ru),style='Selected.Nav.TButton' if key==self.page else 'Nav.TButton')
        self.status.set(self.t('Operation running','Операция выполняется') if self.state['busy'] else self.t('Ready','Готово'))
        self.footer.set(self.state['profile_path'])

    def refresh(self,render=True):
        self.state=self.controller.snapshot();self.config=copy.deepcopy(self.state['config']);self.packages=dict(self.state['packages'])
        self.membership_stamp=self.state.get('membership_stamp')
        if self.role not in self.config['hosts']:self.role=next((r for r in self.config['hosts'] if r.startswith('worker')),'app')
        self.refresh_context()
        if render:self.render()

    def render(self):
        self.mutable=[];self.fields={}
        for var,token in self.traces:var.trace_remove('write',token)
        self.traces=[]
        for widget in self.content.winfo_children():widget.destroy()
        ttk.Label(self.content,text=next(self.t(en,ru) for key,en,ru in self.sections if key==self.page),font=('Segoe UI',23,'bold')).pack(anchor='w',pady=(0,18))
        getattr(self,'build_'+self.page)();self.refresh_context()
        self.root.update_idletasks();self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def navigate(self,page):self.page=page;self.render();self.canvas.yview_moveto(0)
    def change_language(self):
        self.controller.language_set({'language':'ru' if self.lang.get()=='Русский' else 'en'});self.render()

    def site_action(self,action):
        if action=='save_draft':
            self.sync();self.controller.save_draft();self.local_dirty=False;self.refresh();return
        if action=='import_profile':
            path=self.pick('profile')
            if path:self.change_site(action,{'path':path})
        else:self.change_site(action,{})

    def change_site(self,action,payload):
        if self.local_dirty or self.controller.dirty:
            answer=messagebox.askyesnocancel('ProdCast Manager',self.t('Save the non-secret draft before switching? Save server passwords separately.','Сохранить черновик перед переключением? Пароли сохраняются отдельно.'),parent=self.root)
            if answer is None:return
            if answer:self.sync();self.controller.save_draft()
        self.controller.dispatch(action,payload);self.access={};self.sudo_modes={};self.certificate={};self.local_dirty=False
        self.state=self.controller.snapshot();self.directory=dict(self.state['directory']);self.activation=dict(self.state['activation'])
        self.worker_services=copy.deepcopy(self.state.get('worker_services',{}))
        self.step=0;self.refresh()

    def start(self,action,payload=None,sync=True):
        if sync:self.sync()
        value=self.controller.dispatch(action,payload);self.state=self.controller.snapshot()
        if value and value.get('operation'):self.operation=value['operation']
        for w,normal in self.mutable:
            if w.winfo_exists():w.configure(state='disabled' if self.state['busy'] else normal)
        self.refresh_context();return value

    def poll(self):
        if self.closed:return
        if self.after_id:
            try:self.root.after_cancel(self.after_id)
            except tk.TclError:pass
            self.after_id=None
        try:
            old=self.state['busy'];self.state=self.controller.snapshot();result=self.state['result']
            if self.page=='operations':self.update_logs()
            self.refresh_context()
            if not self.state['busy'] and (old or (result and result['operation']!=self.result_seen)):
                self.refresh()
                if result and result['operation']!=self.result_seen:self.result_seen=result['operation'];self.result(result)
        finally:
            if not self.closed:self.after_id=self.root.after(500,lambda:self.guard(self.poll))

    def text_dialog(self,title,text):
        top=tk.Toplevel(self.root);apply_branding(top);top.title('ProdCast Manager — '+title);top.transient(self.root);top.geometry('740x440')
        frame=ttk.Frame(top,padding=16);frame.pack(fill='both',expand=True)
        view=tk.Text(frame,wrap='word',font=('Consolas',10));view.pack(fill='both',expand=True)
        view.insert('1.0',str(text));view.configure(state='disabled')
        ttk.Button(frame,text=self.t('Copy','Копировать'),command=lambda:self.copy(str(text))).pack(side='left',pady=8)
        ttk.Button(frame,text=self.t('Close','Закрыть'),command=top.destroy).pack(side='right',pady=8)
        return top

    def administrator(self):
        from .gui import AdminAccessDialog
        value=self.controller.administrator();AdminAccessDialog(self.root,value['url'],value['username'],value['password'])

    def destroy(self):
        self.closed=True
        for var,token in self.traces:var.trace_remove('write',token)
        self.traces=[]
        if self.after_id:
            try:self.root.after_cancel(self.after_id)
            except tk.TclError:pass
        self.root.destroy()

    def close(self):
        if self.controller.busy:
            self.message(self.t('Operation running','Операция выполняется'),self.t('Wait for the running action before closing.','Дождитесь завершения операции.'));return
        answer=messagebox.askyesnocancel('ProdCast Manager',self.t('Save the non-secret draft before closing? Save passwords separately.','Сохранить черновик без паролей перед закрытием? Пароли сохраняются отдельно.'),parent=self.root)
        if answer is None:return
        if answer:
            try:self.sync();self.controller.save_draft()
            except Exception as error:self.message('Save failed',self.controller.log.redact(str(error)),True);return
        self.destroy()


    def build_overview(self):
        form=self.group('Current site','Текущая площадка')
        for en,ru,value in [('Profile','Профиль',self.state['profile_path']),('Deployment','Установка',self.t('Installed','Установлено') if self.state['installed'] else self.t('Not confirmed locally','Локально не подтверждена')),('Pending operations','Незавершённые операции',len(self.state['pending']))]:
            form.addRow(self.t(en,ru),self.label(value))
        self.note('Local history does not prove server health. Run a status check for current server state.','Локальная история не подтверждает состояние VM. Запустите проверку состояния серверов.')
        self.button(self.content,'Configure servers','Настроить серверы',lambda:self.navigate('servers'),True,False)
        self.button(self.content,'Check remote status','Проверить состояние VM',lambda:self.run('status'))
        self.button(self.content,'Operations and recovery','Операции и восстановление',lambda:self.navigate('operations'),mutable=False)
        form=self.group('Verify existing installation / recover failed completion','Проверка существующей установки / завершение после ошибки')
        self.edit(form,'Exact installed complete release ZIP','ZIP точного установленного релиза',self.packages,'release',browse='zip')
        self.edit(form,'Expected manifest SHA256','Ожидаемый SHA256 манифеста',self.packages,'trusted')
        self.button(form,'Healthcheck and finalize…','Проверить здоровье и завершить…',self.healthcheck,True)
        self.note('Checks all core components without restarting or reinstalling them. If all checks pass, only ownership/completion records are reconciled; AI installation and updates become available. Use the original profile and exact installed release.','Проверяет основной стек без перезапуска и переустановки. При успехе согласуются только записи владельцев/завершения; становятся доступны AI и обновления. Нужны исходный профиль и точный установленный релиз.')

    def healthcheck(self):
        if self.confirm(self.t('Verify existing installation','Проверить существующую установку'),self.t('Verify all core components and finalize the interrupted journal only if they are healthy? No service is restarted, reinstalled or assigned a different account.','Проверить все основные компоненты и завершить журнал только при успешной проверке? Службы не перезапускаются, не переустанавливаются и не меняют учётные записи.')):
            self.start('run',{'mode':'healthcheck'})

    def build_servers(self):
        if self.role not in self.config['hosts']:self.role='app'
        form=self.group('Worker actions','Действия с Workers')
        bar=ttk.Frame(form,style='White.TFrame');form.addRow(bar)
        self.worker_buttons={}
        entries=[('add','Add worker address','Добавить адрес Worker',self.add_worker,True),
            ('install','Install added workers…','Установить добавленных Workers…',lambda:self.worker_action('add-workers'),self.state['installed']),
            ('repair','Repair / reinstall worker…','Восстановить Worker…',lambda:self.worker_action('worker-repair'),self.state['installed'] and self.role in self.state['installed_workers']),
            ('remove','Remove worker…','Удалить Worker…',lambda:self.worker_action('worker-remove'),self.state['installed'] and self.role in self.state['installed_workers'])]
        for index,(key,en,ru,fn,enabled) in enumerate(entries):
            b=ttk.Button(bar,text=self.t(en,ru),command=lambda action=fn:self.guard(action))
            b.grid(row=index//2,column=index%2,sticky='ew',padx=4,pady=4);bar.columnconfigure(index%2,weight=1)
            b.configure(state='normal' if enabled and not self.state['busy'] else 'disabled');self.worker_buttons[key]=b
            self.mutable.append((b,'normal' if enabled else 'disabled'))
        self.label(self.t('Repair/remove require an installed profile and a selected installed Worker. Adding/repairing requires the exact installed release. For a new site, worker addresses are installed with the stack.','Ремонт/удаление требуют установленного профиля и выбранного установленного Worker. Для добавления/ремонта нужен точный установленный релиз. На новой площадке адреса Workers устанавливаются со стеком.'),self.content,True)
        form=self.group('Servers and addresses','Серверы и адреса')
        self.server_tree=ttk.Treeview(form,columns=('ip','user','auth','trust'),show='tree headings',height=min(len(self.config['hosts']),8),selectmode='browse')
        for key,title,width in [('#0',self.t('Server','Сервер'),100),('ip','IPv4',155),('user',self.t('SSH user','Пользователь SSH'),160),('auth',self.t('Access','Доступ'),120),('trust',self.t('Fingerprint','Отпечаток'),135)]:
            self.server_tree.heading(key,text=title);self.server_tree.column(key,width=width,stretch=key!='#0')
        form.addRow(self.server_tree)
        for role in self.config['hosts']:self.server_tree.insert('','end',iid=role,text=role);self.update_server_row(role)
        self.server_tree.selection_set(self.role)
        self.server_tree.bind('<<TreeviewSelect>>',lambda e:self.guard(self.select_server))
        if not self.state['identity_bound']:
            count=ttk.Frame(form,style='White.TFrame')
            self.worker_count=tk.StringVar(master=self.root,value=str(len([r for r in self.config['hosts'] if r.startswith('worker')])))
            for text,delta in [('−',-1),('+',1)]:
                b=ttk.Button(count,text=text,width=3,command=lambda n=delta:self.guard(lambda:self.initial_workers(max(1,min(16,int(self.worker_count.get())+n)))))
                b.pack(side='left',padx=4);self.mutable.append((b,'normal'));b.configure(state='disabled' if self.state['busy'] else 'normal')
                if delta==-1:
                    entry=ttk.Entry(count,textvariable=self.worker_count,width=4,state='readonly');entry.pack(side='left');self.no_wheel(entry)
            form.addRow(self.t('Initial worker count','Начальное число Workers'),count)
        host=self.config['hosts'][self.role];access=self.access.setdefault(self.role,{})
        form=self.group('Server access · '+self.role,'Доступ к серверу · '+self.role);self.editor_card=form
        self.edit(form,'IPv4 address','IPv4-адрес',host,'address','192.0.2.10')
        self.edit(form,'SSH port','Порт SSH',host,'port','22')
        self.edit(form,'SSH username','Пользователь SSH',host,'username','Administrator' if self.role.startswith('worker') else 'ubuntu')
        self.combo(form,'Authentication','Аутентификация',host,'auth',[('key',self.t('SSH private key','Приватный ключ SSH')),('password',self.t('Password','Пароль'))],True)
        saved=self.state['credentials'].get(self.role,{})
        if host['auth']=='password':self.edit(form,'SSH password','Пароль SSH',access,'password',self.t('Saved; empty keeps password','Сохранён; пустое поле оставляет пароль') if saved.get('password') else '',True)
        else:
            self.edit(form,'SSH private key','Приватный ключ SSH',host,'key_path',browse='key')
            self.edit(form,'Key passphrase','Пароль ключа',access,'passphrase','',True)
        if not self.role.startswith('worker'):
            mode=self.sudo_modes.setdefault(self.role,{'mode':'root' if host['username']=='root' else 'password' if saved.get('sudo_password') or access.get('sudo_password') else 'passwordless'})
            values=[('root',self.t('Root account','Пользователь root')),('passwordless',self.t('Passwordless sudo','Sudo без пароля')),('password',self.t('Separate sudo password','Отдельный пароль sudo'))]
            choice=self.combo(form,'Linux privileges','Права Linux',mode,'mode',values)
            def changed_privileges(event=None):
                mode['mode']=values[choice.current()][0]
                if mode['mode']!='password':access['sudo_password']=''
                self.render()
            choice.bind('<<ComboboxSelected>>',lambda e:self.guard(changed_privileges))
            if mode['mode']=='password':self.edit(form,'sudo password','Пароль sudo',access,'sudo_password',self.t('Saved; empty keeps password','Сохранён; пустое поле оставляет пароль') if saved.get('sudo_password') else '',True)
        self.edit(form,'Trusted fingerprint','Доверенный отпечаток',host,'fingerprint',readonly=True)
        self.button(form,'Save server access','Сохранить доступ',self.save_access,True)
        self.button(form,'Clear saved access','Очистить доступ',self.clear_access)
        self.button(form,'Retrieve SSH fingerprint…','Получить SSH-отпечаток…',lambda:self.start('scan_server',{'role':self.role}))
        self.button(form,'Test selected server','Проверить выбранный сервер',lambda:self.start('check_server',{'role':self.role}))
        if self.role.startswith('worker'):
            account=self.worker_services.setdefault(self.role,{'mode':'preserve','account':''})
            form=self.group('Worker Windows service account (not SSH)','Учётная запись службы Worker (не SSH)')
            self.combo(form,'Service logon','Вход службы',account,'mode',[('preserve',self.t('Preserve existing; managed account for a new service','Сохранить существующую; управляемая для новой службы')),('custom',self.t('Dedicated local/domain service account','Выделенная локальная/доменная учётная запись')),('managed',self.t('Explicitly use local prodcast-managed','Явно использовать локальную prodcast-managed'))],True)
            self.fields['service_mode']=self.fields.pop('mode')
            if account.get('mode')=='custom':
                self.edit(form,'Service account','Учётная запись службы',account,'account','DOMAIN\\svc-prodcast or worker@example.com')
                ssh_password_field=self.fields.get('password')
                self.edit(form,'Service password','Пароль службы',account,'password',self.t('Stored; empty keeps password','Сохранён; пустое поле оставляет пароль') if account.get('password_stored') else '',True)
                self.fields['service_password']=self.fields.pop('password')
                if ssh_password_field is not None:self.fields['password']=ssh_password_field
            self.button(form,'Save service account settings…','Сохранить настройки учётной записи…',self.save_worker_service,True)
            self.note('Credentials are encrypted separately from SSH. The service account must have effective Log on as a service rights. Saving does not change a running service; changes apply only when this Worker is explicitly installed/updated/repaired. Preserve never silently replaces an existing account.','Данные шифруются отдельно от SSH. Учётная запись должна иметь действующее право «Вход в качестве службы». Сохранение не меняет работающую службу; настройки применяются только при установке/обновлении/ремонте этого Worker. Сохранение существующей записи не подменяет её автоматически.')

    def save_worker_service(self):
        value={k:v for k,v in self.worker_services[self.role].items() if k!='password_stored'}
        if not value.get('password'):value.pop('password',None)
        if value.get('mode')!='preserve' and not self.confirm(self.t('Service account change','Изменение учётной записи службы'),self.t('Save this explicit service logon choice? It applies only during a reviewed action on this Worker.','Сохранить явный выбор учётной записи? Он применяется только при подтверждённой операции с этим Worker.')):return
        self.controller.worker_service_save(dict(value,role=self.role))
        self.worker_services[self.role]=self.controller.snapshot()['worker_services'][self.role];self.render()

    def update_server_row(self,role):
        host=self.config['hosts'][role]
        self.server_tree.item(role,values=(host['address'] or self.t('Not configured','Не настроен'),host['username'],
            self.t('Password','Пароль') if host['auth']=='password' else self.t('SSH key','Ключ SSH'),
            self.t('Trusted','Подтверждён') if host.get('fingerprint') else self.t('Not verified','Не проверен')))

    def select_server(self):
        selected=self.server_tree.selection()
        if selected and selected[0]!=self.role:self.role=selected[0];self.render();self.scroll_to_editor()

    def scroll_to_editor(self):
        self.root.update_idletasks()
        if self.page=='servers':self.canvas.yview_moveto(self.editor_card.winfo_y()/max(1,self.content.winfo_height()))

    def build_deploy(self):
        form=self.group('Deployment action','Операция развёртывания')
        self.combo(form,'Action','Действие',self.deploy,'mode',[('install',self.t('New installation','Новая установка')),('update',self.t('Update installed site','Обновление площадки'))])
        self.label(' → '.join(self.t(en,ru) if i!=self.step else '[ '+self.t(en,ru)+' ]' for i,(en,ru) in enumerate([('Site','Площадка'),('Package','Пакет'),('AI & settings','AI и настройки'),('Review','Обзор')])),self.content,True)
        if self.step==0:
            form=self.group('Site','Площадка')
            for en,ru,key,hint in [('Site ID','ID площадки','site_id','prodcast-production'),('Initial administrator username','Начальный логин администратора','admin_username','admin'),('Administrator email','Email администратора','admin_email','admin@example.com')]:self.edit(form,en,ru,self.config,key,hint)
            self.button(form,'Configure servers and access','Настроить серверы и доступ',lambda:self.navigate('servers'),mutable=False)
            self.button(form,'App settings','Настройки App',lambda:self.navigate('settings'),mutable=False)
        elif self.step==1:self.package_form()
        elif self.step==2:
            form=self.group('AI handling during update','AI при обновлении')
            self.combo(form,'AI action','Действие AI',self.packages,'ai_action',[('keep',self.t('Keep existing AI unchanged','Сохранить существующий AI')),('update',self.t('Install / update AI packages','Установить / обновить AI'))],True)
            if self.packages['ai_action']=='update' or self.deploy['mode']=='install':self.ai_files()
            else:self.note('Omitting AI packages is intentional. No new AI deployment will be left pending.','Пропуск AI-пакетов намеренный. Новая незавершённая операция AI не создаётся.')
        else:
            form=self.group('Review before starting','Обзор перед запуском')
            if self.deploy['mode']=='update':
                self.button(form,'Preview changed services','Показать изменённые службы',lambda:self.start('update_preview'))
                self.note('Healthcheck is optional. Older profiles without component receipts use a full-stack update, including all Workers. After success, future updates can skip unchanged components. Preview the scope before starting.','Проверка здоровья необязательна. Для старого профиля без данных компонентов выполняется обновление всего стека, включая всех Workers. После успеха следующие обновления смогут пропускать неизменённые компоненты. Перед запуском проверьте состав обновления.')
            form.addRow(self.t('Release','Релиз'),self.label(self.packages['release'] or '—'))
            for role,host in self.config['hosts'].items():form.addRow(role,self.label(host['address'] or self.t('Required','Обязательно')))
            self.note('Installation/update checks ownership, access and package compatibility before mutation. Updates stop services. Database rollback is not automatic.','Перед изменениями проверяются владельцы, доступ и совместимость пакета. Обновление останавливает службы. Автоотката БД нет.')
            self.button(form,'Review and start…','Проверить и запустить…',lambda:self.run(self.deploy['mode']),True)
        bar=ttk.Frame(self.content);bar.pack(fill='x',pady=10)
        for en,ru,delta in [('Back','Назад',-1),('Next','Далее',1)]:
            b=ttk.Button(bar,text=self.t(en,ru),command=lambda d=delta:self.wizard_step(d))
            b.pack(side='left',padx=5)
            if not 0<=self.step+delta<=3:b.configure(state='disabled')

    def wizard_step(self,delta):self.step=max(0,min(3,self.step+delta));self.render();self.canvas.yview_moveto(0)

    def build_settings(self):
        bar=ttk.Frame(self.content);bar.pack(fill='x',pady=(0,14))
        for key,en,ru in [('general','General & network','Основные и сеть'),('https','HTTPS','HTTPS'),('directory','Domain login','Доменный вход'),('activation','Activation','Активация')]:
            ttk.Button(bar,text=self.t(en,ru),style='Primary.TButton' if key==self.setting else 'TButton',command=lambda k=key:self.settings_tab(k)).pack(side='left',padx=3)
        getattr(self,'settings_'+self.setting)()

    def settings_tab(self,key):self.setting=key;self.render();self.canvas.yview_moveto(0)

    def settings_directory(self):
        d=self.directory;form=self.group('LDAP / Active Directory','LDAP / Active Directory')
        self.check(form,'Enable domain login','Включить доменный вход',d,'enabled')
        d['transport']='ldap' if d.get('url','').startswith('ldap://') else 'ldaps'
        w=self.combo(form,'Connection security','Защита соединения',d,'transport',[('ldaps','LDAPS / TLS'),('ldap',self.t('LDAP / unencrypted','LDAP / без шифрования'))])
        def transport(event=None):
            from urllib.parse import urlsplit
            kind=('ldaps','ldap')[w.current()];old=urlsplit(d.get('url',''))
            d['url']=kind+'://'+((old.hostname+':'+str(389 if kind=='ldap' else 636)) if old.hostname else '')
            self.render()
        w.bind('<<ComboboxSelected>>',lambda e:self.guard(transport))
        self.edit(form,'LDAP / LDAPS URL','Адрес LDAP / LDAPS',d,'url',d['transport']+'://dc01.example.com:'+('389' if d['transport']=='ldap' else '636'))
        if d['transport']=='ldap':
            self.check(form,'I allow unencrypted LDAP. Passwords are sent without encryption.','Разрешаю LDAP без шифрования. Пароли передаются открытым текстом.',d,'allow_plain_ldap')
        else:
            self.button(form,'Load CA certificate…','Загрузить сертификат CA…',self.load_directory_ca)
            self.button(form,'Use system CA trust','Использовать системные CA',self.system_ca)
            self.label(self.t('CA loaded; hostname and certificate checks remain required.','CA загружен; проверка имени и сертификата обязательна.') if d.get('ca_pem') else self.t('VM diagnostics and container login may use different trust stores.','Диагностика VM и вход в контейнере могут использовать разные хранилища CA.'),self.content,True)
        form=self.group('Directory lookup and access','Поиск и допуск пользователей')
        for en,ru,key,hint in [('User domain','Домен пользователей','domain','example.com'),('Search Base DN','Base DN поиска','base_dn','DC=example,DC=com'),('Allowed group DN','DN группы допуска','group_dn','CN=ProdCast-Users,OU=Groups,DC=example,DC=com'),('Search account (optional)','Учётная запись поиска (необязательно)','bind_dn','svc@example.com')]:self.edit(form,en,ru,d,key,hint)
        self.edit(form,'Search password (optional)','Пароль поиска (необязательно)',d,'bind_password',self.t('Saved; leave empty to keep','Сохранён; пустое поле оставляет пароль') if d.get('password_stored') else '',True)
        self.combo(form,'User login format','Формат логина',d,'user_search_attribute',[('userPrincipalName','userPrincipalName'),('sAMAccountName','sAMAccountName')])
        self.note('Leave both search-account fields empty to use the signing-in user. Group restrictions remain. For directory checks without a search account, supply temporary test credentials below. Clearing the account name removes its stored password.','Оставьте поля поиска пустыми для входа от имени пользователя. Группы допуска проверяются. Для диагностики без служебной записи введите ниже временный логин/пароль. Очистка имени удаляет сохранённый пароль.')
        self.button(form,'Test connection','Проверить соединение',lambda:self.probe('connection'))
        self.button(form,'Test lookup and group','Проверить поиск и группу',lambda:self.probe('directory'))
        form=self.group('Test user access (credentials never saved)','Пробный допуск пользователя (пароль не сохраняется)')
        self.edit(form,'Test login / UPN','Пробный логин / UPN',d,'test_user')
        self.edit(form,'Test password','Пробный пароль',d,'test_password','',True)
        self.button(form,'Test user','Проверить пользователя',lambda:self.probe('user'))
        self.button(self.content,'Save for next deployment','Сохранить для следующего развёртывания',self.save_directory,True)

    def build_operations(self):
        form=self.group('History and diagnostics','История и диагностика')
        self.history_ids=[x['id'] for x in self.state['history']]
        self.history_names=[x.get('mode','')+' · '+x['id'] for x in self.state['history']]
        if not self.operation and self.history_ids:self.operation=self.history_ids[0]
        self.history_value=tk.StringVar(master=self.root,value=self.history_names[self.history_ids.index(self.operation)] if self.operation in self.history_ids else '')
        self.history_combo=ttk.Combobox(form,textvariable=self.history_value,values=self.history_names,state='readonly');self.no_wheel(self.history_combo)
        form.addRow(self.t('Operation (including core)','Операция (включая основной стек)'),self.history_combo)
        self.history_combo.bind('<<ComboboxSelected>>',lambda e:self.choose_operation())
        self.target_value=tk.StringVar(master=self.root,value='')
        self.target=ttk.Combobox(form,textvariable=self.target_value,values=('',),state='readonly');self.no_wheel(self.target)
        self.target.bind('<<ComboboxSelected>>',lambda e:self.update_logs());form.addRow(self.t('Target (blank = all)','Сервер (пусто = все)'),self.target)
        self.summary=tk.BooleanVar(master=self.root,value=False);self.follow=tk.BooleanVar(master=self.root,value=True)
        for value,en,ru in [(self.summary,'Summary only','Только сводка'),(self.follow,'Follow output','Следить за журналом')]:
            form.addRow(ttk.Checkbutton(form,text=self.t(en,ru),variable=value,command=self.update_logs))
        self.log_view=tk.Text(self.content,height=16,wrap='none',font=('Consolas',10),background='white',relief='solid',borderwidth=1)
        self.log_view.pack(fill='both',expand=True,pady=(0,12));self.log_view.configure(state='disabled')
        self.button(self.content,'Copy displayed log','Копировать отображаемый журнал',lambda:self.copy(self.log_view.get('1.0','end-1c')),mutable=False)
        self.button(self.content,'Open logs folder','Открыть папку журналов',self.open_logs,mutable=False)
        self.button(self.content,'Inspect older session log…','Открыть журнал прежнего сеанса…',self.legacy_log,mutable=False)
        self.button(self.content,'Inspect pending operations','Незавершённые операции',lambda:self.text_dialog(self.t('Recovery','Восстановление'),json.dumps(self.controller.snapshot()['pending'],ensure_ascii=False,indent=2)),mutable=False)
        self.button(self.content,'Stop previous core / AI operation…','Остановить предыдущую основную / AI операцию…',self.stop_previous)
        self.note('Stop releases supported core/AI owners; it is not database rollback. Other journals retain their supported resume actions.','Остановка освобождает владельцев основной/AI операции; это не откат БД. Для остальных журналов используйте исходные операции.')
        self.update_logs()

    def choose_operation(self):
        i=self.history_combo.current()
        self.operation=self.history_ids[i] if i>=0 else '';self.update_logs()

    def update_logs(self):
        value=self.controller.logs({'id':self.operation,'target':self.target_value.get(),'summary':self.summary.get()})
        self.target.configure(values=('',)+tuple(value['targets']))
        if self.log_view.get('1.0','end-1c')!=value['text']:
            position=self.log_view.yview();self.log_view.configure(state='normal')
            self.log_view.delete('1.0','end');self.log_view.insert('1.0',value['text']);self.log_view.configure(state='disabled')
            if self.follow.get():self.log_view.see('end')
            else:self.log_view.yview_moveto(position[0])

    def open_logs(self):
        if sys.platform=='win32':os.startfile(str(self.controller.log.directory))

    def legacy_log(self):
        path=self.pick('log')
        if not path:return
        file=Path(path).resolve()
        if not file.is_relative_to(self.controller.log.directory.resolve()):raise ValueError('Choose a file in Manager logs')
        if file.stat().st_size>8*1024*1024:raise ValueError('Open large logs from the logs folder')
        self.text_dialog(self.t('Previous session','Прежний сеанс'),self.controller.log.redact(file.read_text('utf-8',errors='replace')))

    def run(self,mode):
        descriptions={
            'install':self.t('Install the selected complete release on the configured servers?','Установить выбранный релиз на настроенные серверы?'),
            'update':self.t('App and scheduling will stop. Backups are created; database rollback is not automatic. Keep existing AI means AI packages are deliberately skipped. Continue?','App и планировщик остановятся. Будут созданы бэкапы; автоотката БД нет. «Сохранить AI» означает намеренный пропуск AI-пакетов. Продолжить?'),
            'repair':self.t('Use the exact installed release. Services may restart; data is retained. Continue?','Нужен точный установленный релиз. Службы могут перезапуститься; данные сохраняются. Продолжить?'),
            'app-tls':self.t('Apply the saved certificate and restart App web services?','Применить сертификат и перезапустить веб-службы App?')}
        if mode in descriptions and not self.confirm(self.t('Review operation','Подтверждение операции'),descriptions[mode]):return
        self.start('run',{'mode':mode})


    def initial_workers(self,count):
        self.sync();self.controller.set_initial_workers({'count':count});self.refresh()


    def add_worker(self):
        self.sync();self.role=self.controller.add_worker_draft()['role'];self.refresh();self.scroll_to_editor()


    def save_access(self):
        self.sync();secrets={k:v for k,v in self.access.get(self.role,{}).items() if v}
        if self.access.get(self.role,{}).get('sudo_password')=='':secrets['sudo_password']=''
        self.controller.save_access({'role':self.role,'secrets':secrets});self.access[self.role]={};self.refresh()
        self.toast(self.t('Access saved encrypted on this PC.','Доступ сохранён зашифрованным на этом ПК.'),7000)


    def clear_access(self):
        if self.confirm(self.t('Clear saved access','Очистить доступ'),self.t('Remove saved passwords for this server?','Удалить сохранённые пароли этого сервера?')):
            self.controller.save_access({'role':self.role,'secrets':dict.fromkeys(('password','passphrase','sudo_password'),'')})
            self.access[self.role]={};self.refresh()


    def worker_action(self,mode):
        if self.confirm(self.t('Review worker action','Операция с Worker'),mode+' · '+self.role+'\n'+self.t('Services may stop. Removal changes stack membership. Keep profile and logs on failure.','Службы могут остановиться. Удаление меняет состав стека. При ошибке сохраните профиль и журналы.')):
            self.start('worker_action',{'mode':mode,'role':self.role})


    def package_form(self):
        form=self.group('Release package','Пакет релиза')
        self.edit(form,'Complete release ZIP','Полный релиз ZIP',self.packages,'release',browse='zip')
        self.edit(form,'Expected manifest SHA256','Ожидаемый SHA256 манифеста',self.packages,'trusted',self.t('Hash of release-manifest.json, not the ZIP','Хэш release-manifest.json, не ZIP'))
        self.button(form,'Verify package locally','Проверить пакет локально',lambda:self.start('verify_release'))


    def ai_files(self):
        form=self.group('Offline AI packages','Офлайн-пакеты AI')
        self.edit(form,'AI models ZIP','ZIP с моделями AI',self.packages,'ai_model',browse='models')
        self.edit(form,'Ollama components ZIP','ZIP компонентов Ollama',self.packages,'ai_ollama',browse='zip')


    def build_ai(self):
        form=self.group('AI server','Сервер AI');self.check(form,'Enable App connection to AI','Включить подключение App к AI',self.config,'install_ai')
        self.note('Stack updates preserve an installed AI and its connection by default. Keep existing AI skips packages, not integration. If a previous update disabled the connection, the AI action restores DB access and App settings without reinstalling models or changing Workers. App services may restart briefly.','Обновление стека сохраняет установленный AI и подключение к нему. «Сохранить существующий AI» пропускает пакеты, а не отключает интеграцию. Если прошлое обновление отключило подключение, действие AI восстановит доступ к БД и настройки App без переустановки моделей и изменения Workers. Возможен краткий перезапуск служб App.')
        self.package_form();self.ai_files()
        self.note('Models ZIP may contain the chat and embedding GGUF files. AI retry is separate from core update.','ZIP моделей может содержать GGUF-файлы чата и эмбеддинга. Повтор AI выполняется отдельно от основного обновления.')
        self.button(self.content,'Install / retry AI','Установить / повторить AI',lambda:self.run('ai'),True)


    def settings_general(self):
        form=self.group('Network and company','Сеть и компания')
        for en,ru,key,hint in [('Public HTTPS URL','Публичный HTTPS URL','public_url','https://prodcast.example.com'),('Management PC IPv4','IPv4 управляющего ПК','admin_ip','192.0.2.100'),('App subnet','Подсеть App','app_subnet','172.30.23.0/24'),('Company ID','ID компании','tenant_id','company-id'),('Company name','Название компании','company_name','Company')]:self.edit(form,en,ru,self.config,key,hint)
        self.edit(form,'Initial administrator username','Начальный логин администратора',self.config,'admin_username','admin')
        self.edit(form,'Administrator email','Email администратора',self.config,'admin_email','admin@example.com')
        self.button(form,'Save local profile','Сохранить локальный профиль',self.save_profile,True)
        self.note('Saving is local. Apply App settings with installation/update; certificate replacement has a separate action.','Сохранение локальное. Настройки App применяются установкой/обновлением; сертификат — отдельной операцией.')


    def save_profile(self):self.sync();self.controller.save_profile();self.local_dirty=False;self.refresh()


    def settings_https(self):
        saved=self.state['certificate']
        if saved:self.label(json.dumps(saved,ensure_ascii=False,indent=2),self.content)
        form=self.group('Organization HTTPS certificate','HTTPS-сертификат организации')
        self.edit(form,'HTTPS URL','HTTPS URL',self.certificate,'url',self.config['public_url'])
        self.edit(form,'PFX / P12 certificate','Сертификат PFX / P12',self.certificate,'path',browse='pfx')
        self.edit(form,'PFX password','Пароль PFX',self.certificate,'password','',True)
        self.edit(form,'Additional CA chain (optional)','Дополнительная цепочка CA',self.certificate,'ca_path',browse='ca')
        self.button(form,'Validate and import PFX…','Проверить и импортировать PFX…',self.import_certificate,True)
        self.button(form,'Apply saved certificate…','Применить сохранённый сертификат…',lambda:self.run('app-tls'))
        self.note('Browser trust requires correct DNS, certificate names and client CA trust. Applying restarts App web services.','Доверие браузера требует правильных DNS, имени сертификата и доверенного CA на клиенте. Применение перезапускает веб-службы App.')


    def import_certificate(self):
        self.sync();value=self.controller.certificate_check(self.certificate);self.certificate.pop('password',None)
        if self.confirm(self.t('Save validated certificate','Сохранить проверенный сертификат'),json.dumps(value,ensure_ascii=False,indent=2)):
            self.controller.certificate_save({'id':value['id']});self.refresh()


    def directory_value(self):
        value={k:v for k,v in self.directory.items() if k not in ('test_user','test_password','password_stored','transport')}
        if not value.get('bind_password'):value.pop('bind_password',None)
        return value


    def load_directory_ca(self):
        path=self.pick('ca')
        if path:self.directory['ca_pem']=self.controller.directory_ca({'path':path});self.render()


    def system_ca(self):self.directory['ca_pem']='';self.render()


    def save_directory(self):
        self.controller.directory_save(self.directory_value());self.directory.pop('bind_password',None)
        self.directory.pop('test_password',None);self.state=self.controller.snapshot()
        self.directory.update(self.state['directory']);self.render()
        self.toast(self.t('Saved locally; apply with the next deployment.','Сохранено локально; применится при следующем развёртывании.'),7000)


    def probe(self,mode):
        value={'mode':mode,'directory':self.directory_value(),'user':self.directory.get('test_user',''),'password':self.directory.get('test_password','')}
        self.start('directory_probe',value);self.directory.pop('test_password',None)
        if 'test_password' in self.fields:self.fields['test_password'].clear()


    def settings_activation(self):
        a=self.activation;a.setdefault('mode','online');form=self.group('Licensing','Активация')
        self.combo(form,'Activation method','Способ активации',a,'mode',[('online',self.t('Online server','Онлайн-сервер')),('offline',self.t('Offline license','Офлайн-лицензия'))],True)
        if a['mode']=='offline':
            self.edit(form,'Issued .license file','Выданный файл .license',a,'document_path',browse='license')
            self.edit(form,'Signing public key PEM','Публичный ключ подписи PEM',a,'public_key_path',browse='public-key')
            self.note('Choose files only when replacing a saved license. Never supply the signing private key.','Выберите файлы для замены сохранённой лицензии. Приватный ключ подписи загружать нельзя.')
        else:
            self.edit(form,'Company ID','ID компании',a,'tenant_id',self.config['tenant_id'])
            self.edit(form,'HTTPS licensing server','HTTPS-сервер лицензий',a,'url','https://licenses.example.com')
            self.edit(form,'Company client token','Клиентский токен компании',a,'token','',True)
            self.edit(form,'Optional licensing CA','CA сервера лицензий',a,'ca_path',browse='ca')
        self.button(form,'Save activation','Сохранить активацию',self.save_activation,True)
        self.button(form,'Show / create installation ID','Показать / создать ID установки',self.identity)


    def save_activation(self):
        self.sync();value={k:v for k,v in self.activation.items() if k not in ('token_stored','offline_stored')}
        if not value.get('token'):value.pop('token',None)
        self.controller.activation_save(value);self.activation.pop('token',None);self.refresh()


    def identity(self):
        self.sync();value=self.controller.initialize_identity();self.refresh()
        self.text_dialog(self.t('Installation ID','ID установки'),value['installation_id'])


    def build_backups(self):
        self.note('A server backup is not a portable encrypted archive. Restore replaces data; reset is not a database rollback. Keep the original profile and logs.','Бэкап на сервере — не переносимый зашифрованный архив. Восстановление заменяет данные; сброс не является откатом БД. Сохраняйте исходный профиль и журналы.')
        self.note('Backups keep the database, workflow files and uploaded data sources. Worker installations and model files (models_files/, official_models/) are excluded. Back up models separately or import them again from SMB. New archives preserve existing model directories during restore and require this Manager version or newer.','Бэкапы сохраняют базу данных, файлы workflows и загруженные источники данных. Установки Workers и файлы моделей (models_files/, official_models/) исключены. Сохраняйте модели отдельно или импортируйте их из SMB. При восстановлении новых архивов существующие папки моделей сохраняются; требуется эта или более новая версия Manager.')
        form=self.group('Portable archive','Переносимый архив')
        self.edit(form,'Archive path','Путь архива',self.maintenance,'path',browse='backup')
        self.button(form,'Choose export destination…','Выбрать путь экспорта…',self.export_path)
        self.edit(form,'Archive password (12+ characters)','Пароль архива (от 12 символов)',self.maintenance,'password','',True)
        self.edit(form,'Saved site ID (full reset only)','Сохранённый ID площадки (полный сброс)',self.maintenance,'confirmation')
        for mode,en,ru in [('backup','Create server backup','Создать бэкап на серверах'),('export','Export encrypted .pcbackup','Экспорт зашифрованного .pcbackup'),('restore','Restore data from archive…','Восстановить данные из архива…'),('import','Recover Manager profile…','Восстановить профиль Manager…'),('repair','Repair installed components…','Восстановить компоненты…'),('reset-keep','Recreate components; keep data…','Пересоздать компоненты с данными…'),('reset-full','Delete installation data…','Удалить данные установки…')]:
            self.button(self.content,en,ru,lambda m=mode:self.maintenance_run(m))


    def export_path(self):
        path=self.pick('backup-save')
        if path:self.maintenance['path']=path;self.render()


    def maintenance_run(self,mode):
        if mode in ('backup','repair'):self.run(mode);return
        if not self.confirm(self.t('Review maintenance','Подтверждение обслуживания'),mode+'\n'+self.t('Restore/reset may stop services and replace or delete data. No automatic database rollback. Continue?','Восстановление/сброс может остановить службы, заменить или удалить данные. Автоотката БД нет. Продолжить?')):return
        self.start('maintenance',dict(self.maintenance,mode=mode));self.maintenance.pop('password',None)
        if 'password' in self.fields:self.fields['password'].clear()


    def stop_previous(self):
        if self.confirm(self.t('Stop previous operation','Остановить предыдущую операцию'),self.t('Release supported core/AI ownership? This does not roll back the database.','Освободить владельцев основной/AI операции? Это не откатывает БД.')):self.start('stop',sync=False)


    def result(self,result):
        if not result['ok']:
            self.message(self.t('Operation needs attention','Операция требует внимания'),result.get('error','')+'\n'+self.t('Keep the profile and logs. Inspect Operations before retrying.','Сохраните профиль и журналы. Перед повтором проверьте страницу «Операции».'),True);return
        value=result.get('value') or {}
        if result['mode']=='fingerprint':
            if self.confirm(self.t('Compare fingerprint with the VM console','Сверьте отпечаток с консолью VM'),str(value.get('address'))+'\n'+str(value.get('value'))+'\n'+self.t('Only continue after independently comparing the complete fingerprint.','Продолжайте только после независимой сверки полного отпечатка.')):
                self.controller.trust_server({'role':value['role'],'fingerprint':value['value']});self.refresh()
        elif result['mode'].startswith('directory-'):
            title=self.t('LDAP test successful','Проверка LDAP успешна') if value.get('ok') else self.t('LDAP test failed','Ошибка проверки LDAP')
            self.text_dialog(title,json.dumps(value,ensure_ascii=False,indent=2))
        elif result['mode']=='export' and value.get('status')=='complete':
            size=value.get('bytes',0)
            size_text=(f'{size/1024/1024:.2f} MiB' if size>=1024*1024 else f'{size/1024:.1f} KiB')
            self.message(self.t('Backup completed','Резервная копия создана'),
                self.t('Backup completed successfully.','Резервное копирование успешно завершено.')+'\n\n'+
                self.t('File: ','Файл: ')+value['archive']+'\n'+self.t('Size: ','Размер: ')+size_text+'\n\n'+
                self.t('Previously running App and Worker services have been restored. Model files are excluded; keep their backups separately.','Ранее работавшие службы App и Worker восстановлены. Файлы моделей исключены; сохраняйте их отдельно.'))
        elif result['mode']=='backup':
            steps=value.get('result',[])
            paths=[str(item['role'])+': '+str(item['result']['path']) for item in steps if isinstance(item,dict) and item.get('action')=='backup' and isinstance(item.get('result'),dict) and item['result'].get('path')]
            self.message(self.t('Server backup completed','Бэкапы на серверах созданы'),
                self.t('Server backups completed successfully.','Резервное копирование на серверах успешно завершено.')+'\n\n'+'\n'.join(paths)+'\n\n'+
                self.t('These backups remain on the servers. Use Export encrypted .pcbackup to save an archive on this PC.','Эти копии остаются на серверах. Для сохранения архива на этом ПК используйте экспорт .pcbackup.'))
        else:self.text_dialog(self.t('Operation complete','Операция завершена'),json.dumps(value,ensure_ascii=False,indent=2))


def smoke_test(home=None):
    import gc
    gc.collect()
    with tempfile.TemporaryDirectory(prefix='manager-tk-smoke-',dir=home) as folder:
        root=tk.Tk();root.withdraw()
        window=None
        try:
            window=DesktopWindow(root,DesktopController(Path(folder)))
            assert len(window.nav)==7
            for key,_,_ in window.sections:window.navigate(key)
            for tab in ('general','https','directory','activation'):
                window.navigate('settings');window.settings_tab(tab)
            window.navigate('servers')
            assert window.fields['address'].get()==''
            assert window.fields['address'].example=='192.0.2.10'
            assert len(window.server_tree.get_children())==len(window.config['hosts'])
            assert all(key in window.worker_buttons for key in ('add','install','repair','remove'))
            assert not any(name.startswith(('PySide6','shiboken6')) for name in sys.modules)
            root.update_idletasks()
        finally:
            if window:window.destroy()
            else:root.destroy()
    return True


def main():
    from .instance import manager_instance
    from .session_logs import application_directory
    root=tk.Tk();root.withdraw();apply_branding(root,default=True)
    instance=manager_instance(application_directory())
    try:instance.__enter__()
    except OSError as error:
        messagebox.showinfo('ProdCast Manager','Manager is already open for this prodcast-data folder, or the folder cannot be locked. Close the existing window and check folder access.\n\n'+str(error),parent=root)
        root.destroy();return 1
    try:
        try:window=DesktopWindow(root,DesktopController())
        except Exception as error:
            messagebox.showerror('ProdCast Manager — startup failed',str(error),parent=root);root.destroy();return 1
        root.deiconify();root.mainloop();return 0
    finally:instance.__exit__(None,None,None)
