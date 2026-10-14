"""Windows desktop setup wizard and sync runner for Blackboard Ultra."""
from __future__ import annotations
import argparse
import json
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'BlackboardSync' / 'data'
os.environ['BBSYNC_HOME'] = str(DATA)
os.environ['PYTHONIOENCODING'] = 'utf-8'
TASK = 'Blackboard Sync Desktop Daily'
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def worker(action, course_id=None):
    DATA.mkdir(parents=True, exist_ok=True)
    # Hold a process-wide lock for both GUI and scheduled operations.
    lock = (DATA / 'operation.lock').open('a+b')
    if os.name == 'nt':
        import msvcrt
        try:
            lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            print('Another login or sync is running. Wait for it to finish.', flush=True)
            return 3
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(DATA/'activity.log', encoding='utf-8')])
    from bbsync.config import Config, CONFIG_PATH
    from bbsync import auth
    from bbsync.client import BBClient, AuthExpired
    from bbsync.manifest import Manifest
    from bbsync.sync import run_sync, update_course_list
    cfg = Config.load()
    try:
        if action == 'login':
            # Preserve SSO profile but stop injecting potentially stale API cookies.
            (DATA / 'session-cookies.json').unlink(missing_ok=True)
        with auth.browser(headless=action != 'login') as ctx:
            user = auth.ensure_session(ctx, cfg.base_url, interactive=action == 'login')
            if not user:
                print('Sign-in required. Use Sign in / Re-login.', flush=True)
                return 2
            client = BBClient(ctx, cfg.base_url)
            if action == 'login':
                update_course_list(client, cfg, user['id'])
                print('Signed in. Select your courses and folders.', flush=True)
            elif action == 'content':
                if course_id not in cfg.courses:
                    raise ValueError('Unknown course.')
                def build(items):
                    rows=[]
                    for item in items:
                        row={'id':item['id'], 'title':item.get('title','Untitled'),
                             'handler':(item.get('contentHandler') or {}).get('id',''), 'children':[]}
                        if item.get('hasChildren'):
                            row['children']=build(client.children(course_id,item['id']))
                        rows.append(row)
                    return rows
                cache=DATA/('content-'+re.sub(r'[^A-Za-z0-9_-]','_',course_id)+'.json')
                cache.write_text(json.dumps(build(client.contents(course_id))),encoding='utf-8')
                print('Course content loaded.',flush=True)
            else:
                stats = run_sync(client, cfg, Manifest.load(), user['id'])
                m = Manifest.load(); m.mark_synced(); m.save()
                print(stats.summary(), flush=True)
                if stats.errors: return 1
        return 0
    except AuthExpired:
        print('Sign-in required. Use Sign in / Re-login.', flush=True)
        return 2
    except Exception as exc:
        # Playwright exceptions include cookie headers; never show or store those.
        message = str(exc).split('Call log:')[0].splitlines()[0]
        if 'EACCES' in str(exc):
            message = 'Download connection denied (EACCES). Check network/security software; login may still be valid.'
        message = re.sub(r'https?://\S+', '[request URL]', message)
        print('Operation failed: ' + message, flush=True)
        return 1
    finally:
        lock.close()


def ps(script):
    return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                          capture_output=True, text=True, creationflags=NO_WINDOW)


def psquote(value):
    return "'" + str(value).replace("'", "''") + "'"


def schedule(hour):
    executable = Path(sys.executable).with_name("pythonw.exe")
    if not executable.exists(): executable = Path(sys.executable)
    script = (
        "$ErrorActionPreference='Stop'; "
        f"$a=New-ScheduledTaskAction -Execute {psquote(executable)} -Argument "
        f"{psquote(chr(34)+str(ROOT/'app.py')+chr(34)+' --scheduled')} -WorkingDirectory {psquote(ROOT)}; "
        f"$t=New-ScheduledTaskTrigger -Daily -At {psquote(hour)}; "
        "$s=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2); "
        "$p=New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) "
        "-LogonType Interactive -RunLevel Limited; "
        f"Register-ScheduledTask -TaskName {psquote(TASK)} -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null"
    )
    result = ps(script)
    if result.returncode: raise RuntimeError('Could not save Windows task. ' + result.stderr[:500])


def unschedule():
    # Enumerate first: looking up a missing task can leave PowerShell with a
    # nonzero exit status even when ErrorAction hides the message.
    script = (
        "$ErrorActionPreference='Stop'; try { "
        "$service=New-Object -ComObject 'Schedule.Service'; $service.Connect(); "
        "$folder=$service.GetFolder('\\'); $found=$false; "
        "foreach($task in $folder.GetTasks(1)){ "
        f"if($task.Name -eq {psquote(TASK)}){{$found=$true;break}} "
        "}; if($found){ "
        f"$folder.DeleteTask({psquote(TASK)},0); Write-Output 'REMOVED' "
        "}else{Write-Output 'ABSENT'}; exit 0 "
        "}catch{Write-Error $_.Exception.Message -ErrorAction Continue; exit 1}"
    )
    result = ps(script)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:700]
        raise RuntimeError('Windows could not disable the scheduled task. '
                           'Setup has not been reset. ' + detail)
    state = result.stdout.strip()
    if state not in ('REMOVED', 'ABSENT'):
        raise RuntimeError('Windows returned an unexpected task status. Setup has not been reset.')
    return state == 'REMOVED'


def main_gui(recovery=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from bbsync.config import Config, CONFIG_PATH
    DATA.mkdir(parents=True, exist_ok=True)
    app = tk.Tk(); app.title('Blackboard Sync · 0.2.3'); app.geometry('1100x800'); app.minsize(1000,700)
    style = ttk.Style(); style.theme_use('clam')
    style.configure('TButton', padding=8)
    style.configure('Title.TLabel', font=('Segoe UI',22,'bold'))
    style.configure('TLabel', font=('Segoe UI',10))
    outer = ttk.Frame(app, padding=20); outer.pack(fill='both', expand=True)
    ttk.Label(outer,text='Blackboard Sync',style='Title.TLabel').pack(anchor='w')
    ttk.Label(outer,text='Sign in once. Choose courses and folders. Keep your study files up to date.').pack(anchor='w',pady=(4,6))
    ttk.Label(outer,text='Developed by Riyan Hashem Jamy').pack(anchor='w',pady=(0,14))
    cfg = Config.load(); busy=False; events=queue.Queue(); resume=False; courses={}; ui_path=DATA/'desktop.json'
    settings = json.loads(ui_path.read_text()) if ui_path.exists() else {}
    url=tk.StringVar(value=cfg.base_url if CONFIG_PATH.exists() else 'https://blackboard.kfupm.edu.sa')
    panel=ttk.LabelFrame(outer,text='1 · Connect to Blackboard',padding=12);panel.pack(fill='x')
    ttk.Entry(panel,textvariable=url).pack(side='left',fill='x',expand=True)
    status=tk.StringVar(value='Ready. Sign in to load your courses.');buttons=[]
    def button(parent,text,command):
        b=ttk.Button(parent,text=text,command=command);buttons.append(b);return b
    def configure_url():
        nonlocal cfg
        value=url.get().strip().rstrip('/')
        parsed=urlsplit(value)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Enter an HTTPS Blackboard URL, without a username or password.')
        base='https://'+parsed.netloc
        cfg=Config.load()
        if cfg.courses and cfg.base_url!=base:
            raise ValueError('Use Start Over before connecting to a different institution.')
        cfg.base_url=base;cfg.save();return base
    def log(text):
        output.configure(state='normal');output.insert('end',text+'\n');output.see('end');output.configure(state='disabled')
    def run(action, course_id=None):
        nonlocal busy
        if busy:return
        try:
            configure_url()
            if action=='sync' and not save_courses():return
        except Exception as exc:messagebox.showerror('Check setup',str(exc));return
        busy=True
        for b in buttons:b.configure(state='disabled')
        status.set('Signing in — complete the browser login.' if action=='login' else 'Loading course folders and items…' if action=='content' else 'Syncing — keep annotation apps closed until OneDrive finishes.')
        def job():
            try:
                runtime=Path(sys.executable).with_name('python.exe')
                command=[str(runtime),str(ROOT/'app.py'),'--worker',action]
                if course_id:command+=['--course',course_id]
                proc=subprocess.Popen(command,stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',creationflags=NO_WINDOW)
                for line in proc.stdout:events.put(('log',line.rstrip()))
                events.put(('done',(action,proc.wait(),course_id)))
            except Exception as exc:events.put(('log',str(exc)));events.put(('done',(action,1,course_id)))
        threading.Thread(target=job,daemon=True).start()
    button(panel,'Sign in / Re-login',lambda:run('login')).pack(side='left',padx=(10,0))
    cp=ttk.LabelFrame(outer,text='2 · Select courses and destination folders',padding=12);cp.pack(fill='both',expand=True,pady=12)
    tree=ttk.Treeview(cp,columns=('enabled','course','folder'),show='headings',height=9,selectmode='browse')
    for name,label,width in [('enabled','Sync',60),('course','Course',350),('folder','Destination folder',430)]:
        tree.heading(name,text=label);tree.column(name,width=width)
    tree.pack(fill='both',expand=True)
    def load_courses():
        nonlocal cfg,courses
        cfg=Config.load();courses={cid: {'name':e.name,'enabled':e.enabled,'destination':e.destination or '',
            'include_content':e.include_content,'include_assignments':e.include_assignments,
            'selected_only':e.selected_only,'content_ids':list(e.content_ids),
            'max_file_mb':e.max_file_mb,'skip_unknown_size':e.skip_unknown_size} for cid,e in cfg.courses.items()}
        tree.delete(*tree.get_children())
        for cid,e in sorted(courses.items(),key=lambda pair:pair[1]['name']):
            tree.insert('', 'end',iid=cid,values=('Yes' if e['enabled'] else 'No',e['name'],e['destination']))
    def selected():
        values=tree.selection()
        if not values:messagebox.showinfo('Select a course','Click a course row first.');return None
        return values[0]
    def render(cid):
        e=courses[cid];tree.item(cid,values=('Yes' if e['enabled'] else 'No',e['name'],e['destination']))
    def toggle():
        if busy:return
        cid=selected()
        if cid:courses[cid]['enabled']=not courses[cid]['enabled'];render(cid)
    def folder():
        if busy:return
        cid=selected()
        if not cid:return
        dest=filedialog.askdirectory(title='Choose the folder for '+courses[cid]['name'])
        if dest:courses[cid]['destination']=dest;courses[cid]['enabled']=True;render(cid)
    tree.bind('<Double-1>',lambda event:toggle())
    row=ttk.Frame(cp);row.pack(fill='x',pady=(8,0))
    button(row,'Enable / Disable',toggle).pack(side='left')
    button(row,'Choose Folder…',folder).pack(side='left',padx=8)
    def save_courses():
        nonlocal cfg
        enabled=[e for e in courses.values() if e['enabled']]
        if not enabled:messagebox.showinfo('Select courses','Enable at least one course.');return False
        roots=[]
        for e in enabled:
            if not e['include_content'] and not e['include_assignments']:
                messagebox.showerror('Content options','Enable course materials, assignments, or both for '+e['name']);return False
            if e['selected_only'] and not e['content_ids']:
                messagebox.showerror('Content options','Choose at least one item for '+e['name']);return False
            if not e['destination'] or not Path(e['destination']).is_dir():
                messagebox.showerror('Choose a folder','Choose an existing folder for '+e['name']);return False
            p=Path(e['destination']).resolve()
            if any(p==r or p.is_relative_to(r) or r.is_relative_to(p) for r in roots):
                messagebox.showerror('Separate folders','Use separate, non-overlapping folders for each course.');return False
            roots.append(p)
        cfg=Config.load()
        for cid,e in courses.items():
            cfg.courses[cid].enabled=e['enabled'];cfg.courses[cid].destination=e['destination'] or None
        for cid,e in courses.items():
            for field in ('include_content','include_assignments','selected_only','content_ids','max_file_mb','skip_unknown_size'):
                setattr(cfg.courses[cid],field,e[field])
        cfg.save();return True
    def content_picker(cid):
        cache=DATA/('content-'+re.sub(r'[^A-Za-z0-9_-]','_',cid)+'.json')
        if not cache.exists():return
        win=tk.Toplevel(app);win.title('Choose content — '+courses[cid]['name']);win.geometry('730x560');win.transient(app);win.grab_set()
        ttk.Label(win,text='Select folders or individual items. Selected folders include future uploads.',wraplength=680).pack(anchor='w',padx=15,pady=12)
        picker=ttk.Treeview(win,columns=('chosen','type'),show='tree headings',height=17)
        picker.heading('#0',text='Course content');picker.heading('chosen',text='Selected');picker.heading('type',text='Type')
        picker.column('#0',width=400);picker.column('chosen',width=70);picker.column('type',width=150)
        picker.pack(fill='both',expand=True,padx=15)
        chosen=set(courses[cid]['content_ids'])
        def add(rows,parent=''):
            for item in rows:
                cid_item=item['id'];handler=item['handler']
                kind='Assignment / assessment' if 'assignment' in handler or 'assessment' in handler else 'Folder' if item['children'] else 'Content'
                picker.insert(parent,'end',iid=cid_item,text=item['title'],values=('Yes' if cid_item in chosen else 'No',kind),open=True)
                add(item['children'],cid_item)
        add(json.loads(cache.read_text(encoding='utf-8')))
        def toggle_item():
            for item_id in picker.selection():
                if item_id in chosen:chosen.remove(item_id)
                else:chosen.add(item_id)
                values=list(picker.item(item_id,'values'));values[0]='Yes' if item_id in chosen else 'No';picker.item(item_id,values=values)
        picker.bind('<Double-1>',lambda event:toggle_item())
        bar=ttk.Frame(win,padding=12);bar.pack(fill='x')
        ttk.Button(bar,text='Select / Unselect',command=toggle_item).pack(side='left')
        def apply():
            if not chosen:messagebox.showinfo('Select items','Choose at least one folder or item.',parent=win);return
            courses[cid]['selected_only']=True;courses[cid]['content_ids']=sorted(chosen);win.destroy()
            messagebox.showinfo('Selection saved','Selection saved in the form. Click Save Choices to persist it.')
        ttk.Button(bar,text='Use Selected Items',command=apply).pack(side='right')
        ttk.Label(win,text='Category settings still apply: an excluded assignment is skipped even within a selected folder.',wraplength=680).pack(padx=15,pady=(0,12))

    def options():
        cid=selected()
        if not cid:return
        e=courses[cid];win=tk.Toplevel(app);win.title('Content options');win.geometry('620x410');win.transient(app);win.grab_set()
        f=ttk.Frame(win,padding=20);f.pack(fill='both',expand=True)
        ttk.Label(f,text=e['name'],wraplength=570).pack(anchor='w',pady=(0,12))
        materials=tk.BooleanVar(value=e['include_content']);assignments=tk.BooleanVar(value=e['include_assignments'])
        specific=tk.BooleanVar(value=e['selected_only']);limit=tk.StringVar(value=str(e['max_file_mb']));unknown=tk.BooleanVar(value=e['skip_unknown_size'])
        ttk.Checkbutton(f,text='Course materials (slides, notes, documents)',variable=materials).pack(anchor='w')
        ttk.Checkbutton(f,text='Assignment attachments',variable=assignments).pack(anchor='w')
        ttk.Checkbutton(f,text='Only selected folders / items',variable=specific).pack(anchor='w',pady=(8,0))
        ttk.Label(f,text=str(len(e['content_ids']))+' selected items; use Choose Content to change them.').pack(anchor='w')
        lr=ttk.Frame(f);lr.pack(fill='x',pady=12)
        ttk.Label(lr,text='Maximum file size (MB; 0 = no limit):').pack(side='left')
        ttk.Spinbox(lr,from_=0,to=100000,textvariable=limit,width=9).pack(side='left',padx=8)
        ttk.Checkbutton(f,text='Skip files with unknown size when a limit is enabled',variable=unknown).pack(anchor='w')
        ttk.Label(f,text='Skipped files appear under Manual Downloads. This filters downloads by reported size; it is not a strict network-data cap.',wraplength=560).pack(anchor='w',pady=10)
        def apply():
            try:value=int(limit.get());assert 0<=value<=100000
            except (ValueError,AssertionError):messagebox.showerror('Size','Enter a whole number from 0 to 100000.',parent=win);return
            e.update(include_content=materials.get(),include_assignments=assignments.get(),selected_only=specific.get(),max_file_mb=value,skip_unknown_size=unknown.get())
            win.destroy()
        ttk.Button(f,text='Apply to Form',command=apply).pack(anchor='e')
    def choose_content():
        cid=selected()
        if not cid:return
        # Preserve unsaved selections before loading the content tree. No sync runs here.
        configure_url()
        c=Config.load()
        for key,e in courses.items():
            for field in ('enabled','destination','include_content','include_assignments','selected_only','content_ids','max_file_mb','skip_unknown_size'):
                setattr(c.courses[key],field,e[field])
        c.save();run('content',cid)
    button(row,'Content Options…',options).pack(side='left',padx=8)
    button(row,'Choose Content…',choose_content).pack(side='left')
    button(row,'Save Choices',save_courses).pack(side='left')
    ttk.Label(cp,text='Use Content Options to include/exclude assignments or limit size; Choose Content selects specific folders. Replaced files are backed up.').pack(anchor='w',pady=(8,0))
    sp=ttk.LabelFrame(outer,text='3 · Sync on demand or daily',padding=12);sp.pack(fill='x')
    button(sp,'Sync Now',lambda:run('sync')).pack(side='left')
    hour=tk.StringVar(value=settings.get('hour','19:00'))
    ttk.Label(sp,text='Daily time (computer timezone):').pack(side='left',padx=(16,5))
    ttk.Entry(sp,textvariable=hour,width=7).pack(side='left')
    def save_schedule():
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',hour.get()):
            messagebox.showerror('Time','Enter a time such as 19:00.');return
        if not save_courses():return
        try:
            schedule(hour.get());ui_path.write_text(json.dumps({'url':url.get(),'hour':hour.get()}))
            messagebox.showinfo('Daily sync enabled','Scheduled at '+hour.get()+'. Windows must be signed in. Missed runs start when available. If login expires, open this app and sign in again.')
        except Exception as exc:messagebox.showerror('Scheduling',str(exc))
    button(sp,'Enable Daily Sync',save_schedule).pack(side='left',padx=8)
    def stop_schedule():
        try:
            removed=unschedule()
            messagebox.showinfo('Schedule','Daily sync disabled.' if removed else 'No daily schedule was enabled. Nothing to disable.')
        except Exception as exc:messagebox.showerror('Schedule',str(exc))
    button(sp,'Disable Schedule',stop_schedule).pack(side='left')
    lower=ttk.Frame(outer);lower.pack(fill='x',pady=10)
    def reset():
        if not messagebox.askyesno('Start Over','Disable daily syncing and clear course choices and login?\n\nYour downloaded files, backups, and download history will be preserved.'):return
        try:
            unschedule()
            c=Config.load()
            for e in c.courses.values():e.enabled=False;e.destination=None
            c.courses={};c.save()
            # Move, rather than delete, authenticated profile; no background process is active.
            import shutil
            from datetime import datetime
            profile=DATA/'browser-profile'
            if profile.exists():shutil.move(str(profile),str(DATA/('profile-before-reset-'+datetime.now().strftime('%Y%m%d-%H%M%S'))))
            (DATA/'session-cookies.json').unlink(missing_ok=True)
            load_courses();status.set('Setup reset. Sign in and choose courses again.')
        except Exception as exc:messagebox.showerror('Start Over',str(exc))
    button(lower,'Start Over…',reset).pack(side='right')
    button(lower,'About / Credits',lambda:messagebox.showinfo('About Blackboard Sync',
        'Blackboard Sync Desktop\n\nDeveloped by Riyan Hashem Jamy\nPortfolio: https://RiyanJamy.github.io\n\nBased on sammyschen/bbsync, licensed under the MIT License.\nhttps://github.com/sammyschen/bbsync\n\nOriginal copyright and license notices are included in LICENSE.\nSee CREDITS.txt for attribution.')).pack(side='right',padx=8)
    def manual_downloads():
        import webbrowser
        from bbsync.manifest import Manifest
        from bbsync.manual import entries, find_existing, download_url, clear
        win=tk.Toplevel(app);win.title('Manual Downloads — failures and exclusions');win.geometry('1050x590');win.transient(app);win.grab_set()
        ttk.Label(win,text='Failed requests and size-skipped files. Existing copies are detected by filename; names alone do not verify Blackboard versions.',wraplength=990).pack(anchor='w',padx=15,pady=12)
        view=ttk.Treeview(win,columns=('course','file','reason','local'),show='headings',height=13)
        for column,title,width in [('course','Course',240),('file','File / item',270),('reason','Reason',240),('local','Local status',230)]:
            view.heading(column,text=title);view.column(column,width=width)
        view.pack(fill='both',expand=True,padx=15)
        records={}
        info=tk.StringVar(value='Select an item to see the destination and download link.')
        ttk.Label(win,textvariable=info,wraplength=990).pack(anchor='w',padx=15,pady=8)
        def refresh():
            nonlocal records
            config=Config.load();records=entries(Manifest.load(),config)
            view.delete(*view.get_children())
            for row_id,state in records.items():
                matches=find_existing(state,config)
                if state.get('kind')!='file':local='Folder contents not verified'
                elif len(matches)>1:local='Multiple matching filenames'
                elif matches:local='Already present — verify' if state.get('existing_at_report',True) else 'New local copy — auto-detect on sync'
                else:local='Not found locally'
                view.insert('', 'end',iid=row_id,values=(state.get('course_name',''),state.get('filename') or state.get('title',''),state.get('reason','Unknown'),local))
            if not records:info.set('No unresolved file failures or size exclusions.')
        def selected_file():
            ids=view.selection()
            if not ids:messagebox.showinfo('Select item','Select a row first.',parent=win);return None
            return ids[0]
        def show_path(event=None):
            row_id=selected_file()
            if row_id:
                state=records[row_id]
                info.set(state.get('title','')+' → '+state.get('path','')+'\n'+download_url(Config.load().base_url,state))
        view.bind('<<TreeviewSelect>>',show_path)
        bar=ttk.Frame(win,padding=15);bar.pack(fill='x')
        def open_link():
            row_id=selected_file()
            if row_id:webbrowser.open(download_url(Config.load().base_url,records[row_id]))
        ttk.Button(bar,text='Download / Open Item',command=open_link).pack(side='left')
        def copy_link():
            row_id=selected_file()
            if row_id:win.clipboard_clear();win.clipboard_append(download_url(Config.load().base_url,records[row_id]))
        ttk.Button(bar,text='Copy Link',command=copy_link).pack(side='left',padx=5)
        def use_file(existing=False):
            row_id=selected_file()
            if not row_id:return
            state=records[row_id]
            if state.get('kind')!='file' or not state.get('attachment_id'):
                messagebox.showinfo('Folder or listing failed','Open this item in Blackboard to inspect its files. Sync Now will retry the listing. Individual filenames were unavailable when this request failed.',parent=win);return
            matches=find_existing(state,Config.load())
            if existing and len(matches)==1:source=str(matches[0])
            else:
                source=filedialog.askopenfilename(parent=win,title='Choose current Blackboard copy of '+state['filename'])
            if not source:return
            if not messagebox.askyesno('Use this copy','Confirm '+Path(source).name+' is the current Blackboard version of '+state['filename']+'.\n\nA filename match does not verify content. Existing destination files will be backed up before replacement.',parent=win):return
            lock=None
            try:
                lock=(DATA/'operation.lock').open('a+b')
                if os.name=='nt':
                    import msvcrt
                    lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
                    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
                current=Manifest.load();latest=entries(current,Config.load()).get(row_id)
                if not latest or latest.get('modified')!=state.get('modified'):
                    raise RuntimeError('Tracking changed. Refresh the list and retry.')
                from bbsync.safe_files import install_staged
                install_staged(source,state['path'])
                current.record_attachment(state['attachment_id'],state['path'],state.get('modified'))
                current._data['attachments'][state['attachment_id']]['manually_confirmed']=True
                clear(current,state['course_id'],state.get('item_id'),state['attachment_id']);current.save()
                refresh();info.set('Local copy accepted. Future unchanged versions will be skipped.')
            except Exception as exc:messagebox.showerror('Could not use file','Close any running sync and retry. '+str(exc),parent=win)
            finally:
                if lock:lock.close()
        ttk.Button(bar,text='Use Existing Copy',command=lambda:use_file(True)).pack(side='left',padx=5)
        ttk.Button(bar,text='Choose Downloaded File…',command=use_file).pack(side='left')
        ttk.Button(bar,text='Refresh',command=refresh).pack(side='left',padx=5)
        def guide():
            messagebox.showinfo('Manual Downloads guide','Download / Open Item opens a stable Blackboard download link when a file is known, or the course outline for a failed folder/listing. Your normal browser must be signed in. If the direct API link is rejected, open the course normally and locate the displayed item.\n\nSave the downloaded file, with its original filename, anywhere inside this course’s destination folder. If no matching local copy existed when the failure was recorded, the next sync recognizes a unique new filename match automatically (content is not verified).\n\nIf a copy was already present, use Use Existing Copy and confirm it is current. Multiple matches need you to choose a file. Never rename/replace an annotated original without keeping a backup.\n\nA filename match cannot prove a remote update is present. Automatic recognition is limited to newly arrived copies; old/annotated copies are never silently accepted as updates.\n\nTo retry normally, close this window and click Sync Now. To change the size filter, use Content Options, Save Choices, then Sync Now. New courses default to 50 MB. Existing size preferences remain unchanged.',parent=win)
        ttk.Button(win,text='How manual recovery works',command=guide).pack(anchor='w',padx=15,pady=(0,12))
        refresh()
    button(lower,'Manual Downloads…',manual_downloads).pack(side='right',padx=8)
    button(lower,'Open App Data',lambda:os.startfile(DATA)).pack(side='right',padx=8)
    ttk.Label(lower,textvariable=status,wraplength=630).pack(side='left',fill='x',expand=True)
    output=tk.Text(outer,height=8,state='disabled',font=('Consolas',9),background='#f4f6f8');output.pack(fill='x')
    load_courses()
    if recovery:
        log(recovery.strip())
        status.set("Scheduled sync stopped. Sign in again if requested, then click Sync Now.")
    def poll():
        nonlocal busy,resume
        while not events.empty():
            kind,value=events.get()
            if kind=='log':log(value)
            else:
                action,code,course_id=value;busy=False
                for b in buttons:b.configure(state='normal')
                if action=='login' and code==0:
                    load_courses();status.set('Signed in. Select courses and choose folders.')
                    if resume:resume=False;run('sync')
                elif action=='content' and code==0:
                    status.set('Course content loaded. Choose folders or items.')
                    content_picker(course_id)
                elif code==2:
                    status.set('Sign-in required.')
                    if action=='sync' and messagebox.askyesno('Sign in required','Open the login browser, then resume this sync?'):
                        resume=True;run('login')
                elif code==0:status.set('Sync complete. Let OneDrive finish uploading before opening slides elsewhere.')
                else:resume=False;status.set('Operation stopped. See the message below.')
        app.after(150,poll)
    def close():
        if busy:messagebox.showinfo('Operation running','Wait for the current operation to finish before closing.');return
        app.destroy()
    app.protocol('WM_DELETE_WINDOW',close);poll();app.mainloop()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--worker',choices=['login','sync','content']);parser.add_argument('--course');parser.add_argument('--scheduled',action='store_true');args=parser.parse_args()
    if args.worker:
        # Keep scheduled output local, but GUI captures worker stdout.
        sys.exit(worker(args.worker,args.course))
    if args.scheduled:
        runtime = Path(sys.executable).with_name("python.exe")
        result = subprocess.run([str(runtime), str(ROOT/'app.py'), '--worker', 'sync'],
                                capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
        if result.returncode == 0:
            sys.exit(0)
        # Windows task uses the interactive user's session, so the GUI can request login.
        # Recovery remains explicit: user presses Sign in, then Sync Now.
    main_gui(result.stdout if args.scheduled else None)
