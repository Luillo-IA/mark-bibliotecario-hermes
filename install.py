#!/usr/bin/env python3
"""Instala MARK en el único Hermes existente, con sudo ordinario y rollback acotado."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
import platform
from pathlib import Path
import pwd
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import yaml

PACKAGE=Path(__file__).resolve().parent
ACCOUNT=Path(os.environ.get('MARK_ACCOUNT_HOME','/var/lib/hermes'))
HOME=ACCOUNT/'.hermes'
PROFILE=HOME/'profiles/mark'
CLI=ACCOUNT/'.local/bin/hermes'
REPO=HOME/'hermes-agent'
SOURCE=Path(os.environ.get('MARK_ACCOUNTS_FILE',str(PACKAGE/'.private/credenciales.json')))
EVIDENCE=Path(os.environ.get('MARK_REPORT_DIR',str(PACKAGE/'reports')))
SERVICE=os.environ.get('MARK_GATEWAY_SERVICE','hermes-gateway.service')
VERSION='mark-biblioteca-v1'
PHASE='preflight'
PUBLIC_REPORT=None
BACKUP=None
SAFE_DIAGNOSTIC=None

# Official Playwright 1.64 Ubuntu 24.04 Chromium dependencies, narrowed to the
# missing ELF libraries of the installed headless shell. No X server is needed.
HEADLESS_PACKAGES={
    'libatk-1.0.so.0':'libatk1.0-0t64',
    'libatk-bridge-2.0.so.0':'libatk-bridge2.0-0t64',
    'libatspi.so.0':'libatspi2.0-0t64',
    'libXcomposite.so.1':'libxcomposite1',
    'libXdamage.so.1':'libxdamage1',
    'libXfixes.so.3':'libxfixes3',
    'libXrandr.so.2':'libxrandr2',
    'libgbm.so.1':'libgbm1',
    'libasound.so.2':'libasound2t64',
}


def check(value, code):
    if not value: raise RuntimeError(code)


def missing_browser_packages(binaries):
    check(bool(binaries),'browser_binary_missing')
    missing=set()
    for binary in binaries:
        linked=subprocess.run(['ldd',str(binary)],capture_output=True,text=True,timeout=20)
        check(linked.returncode==0,'browser_ldd_failed')
        missing.update(re.findall(r'^\s*(\S+)\s+=>\s+not found\s*$',linked.stdout,re.M))
    if not missing: return []
    release=platform.freedesktop_os_release()
    check(release.get('ID')=='ubuntu' and release.get('VERSION_ID')=='24.04','browser_system_version_unsupported')
    check(missing<=HEADLESS_PACKAGES.keys(),'browser_missing_library_unmapped')
    return sorted({HEADLESS_PACKAGES[name] for name in missing})


def safe_failure_details(log_text):
    """Classify private output without returning any of its literal contents."""
    text=log_text.lower()
    categories={
        'permission_denied':('permission denied','eacces'),
        'venv_ensurepip':('ensurepip is not available','no module named ensurepip'),
        'tls_certificate':('certificate_verify_failed','unable_to_verify_leaf_signature','self_signed_cert','unable to get local issuer'),
        'package_unavailable':('no matching distribution','unable to locate package','has no installation candidate'),
        'network':('enotfound','etimedout','econnreset','temporary failure in name resolution','temporary failure resolving','failed to fetch','download failure'),
        'apt_repository':('not signed','no_pubkey','does not have a release file','signatures couldn'),
        'apt_lock':('could not get lock','unable to acquire the dpkg frontend lock'),
        'missing_system_libraries':('error while loading shared libraries','missing dependencies'),
        'playwright_module_missing':('no module named playwright',),
    }
    reasons=[name for name,patterns in categories.items() if any(p in text for p in patterns)]
    return {'reasons':reasons or ['unclassified'],
            'pip_completed':'successfully installed' in text or 'requirement already satisfied: playwright' in text,
            'browser_download_completed':'downloaded to' in text,
            'system_install_started':'installing dependencies...' in text}


def diagnose_last_failure(operator):
    # Read the last installation backup only; never export its raw log or secrets.
    candidates=sorted((ACCOUNT/'migration').glob('mark-biblioteca-*/operations.log'))
    check(bool(candidates),'installation_log_missing')
    source=candidates[-1]
    check(not source.is_symlink() and not source.parent.is_symlink(),'unexpected_symlink')
    with source.open('rb') as stream:
        stream.seek(max(0,source.stat().st_size-262144))
        details=safe_failure_details(stream.read().decode(errors='replace'))
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target=EVIDENCE/('mark-biblioteca-diagnostic-'+stamp+'.json')
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'source':str(source),
            'kind':'safe_installation_diagnostic','details':details,'services_changed':False}
    write(target,json.dumps(report,indent=2)+'\n',operator)
    print(json.dumps(details,ensure_ascii=False))
    print('Informe: '+str(target))


def write(path, content, owner):
    path=Path(path)
    check(not path.is_symlink(),'unexpected_symlink')
    if not path.parent.exists():
        path.parent.mkdir(parents=True,mode=0o700)
        os.chown(path.parent,owner.pw_uid,owner.pw_gid)
    fd,name=tempfile.mkstemp(prefix='.mark-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            os.fchmod(f.fileno(),0o600);os.fchown(f.fileno(),owner.pw_uid,owner.pw_gid)
            f.write(content if isinstance(content,bytes) else content.encode());f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def profile_config(primary):
    result={key:copy.deepcopy(primary[key]) for key in ('model','custom_providers','providers','model_catalog','stt','tts') if key in primary}
    result.update(timezone='Europe/Madrid',fallback_providers=[],
        agent={'max_turns':10,'disabled_toolsets':['terminal','file','skills','browser','web','delegation',
        'code_execution','cronjob','session_search','spotify','mcp']},
        platform_toolsets={'telegram':['mark_biblioteca','memory','tts'],'cli':['mark_biblioteca','memory','tts']},
        plugins={'enabled':['mark-biblioteca']},
        memory={'memory_enabled':True,'user_profile_enabled':True},
        voice={'auto_tts':False},gateway={'parked':True},cron={'script_timeout_seconds':720},
        auxiliary={name:{'provider':'main'} for name in ('compression','session_search','vision')})
    return result


def routed_config(primary, group):
    changed=copy.deepcopy(primary)
    gateway=changed.setdefault('gateway',{})
    gateway['multiplex_profiles']=True
    routes=changed['profile_routes'] if 'profile_routes' in changed else gateway.setdefault('profile_routes',[])
    check(isinstance(routes,list),'routes_format')
    matches=[r for r in routes if r.get('platform')=='telegram' and str(r.get('chat_id'))==group]
    check(not any(r.get('profile')!='mark' for r in matches),'group_collision')
    check(all(r.get('enabled',True) for r in matches),'mark_route_disabled')
    check(not any(r.get('profile')=='mark' and str(r.get('chat_id'))!=group for r in routes),'profile_collision')
    if not matches: routes.append({'name':'mark','platform':'telegram','chat_id':group,'profile':'mark'})
    # The same config owner/location that Otro uses; native loader checks both gates.
    extra=changed.setdefault('platforms',{}).setdefault('telegram',{}).setdefault('extra',{})
    for key in ('group_allowed_chats','free_response_chats'):
        value=extra.setdefault(key,[])
        check(isinstance(value,list),'allowlist_format')
        if group not in [str(v) for v in value]: value.append(group)
    if extra.get('allowed_chats'):
        check(isinstance(extra['allowed_chats'],list),'allowed_chats_format')
        if group not in [str(v) for v in extra['allowed_chats']]: extra['allowed_chats'].append(group)
    return changed


def find_group():
    candidates=set()
    path=HOME/'state.db'
    if path.exists():
        with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as db:
            cols={r[1] for r in db.execute('PRAGMA table_info(sessions)')}
            if {'chat_id','display_name','source'}<=cols:
                candidates.update(str(r[0]) for r in db.execute("SELECT chat_id FROM sessions WHERE source='telegram' AND lower(trim(display_name))='mark'") if str(r[0]).startswith('-'))
    def walk(obj):
        if isinstance(obj,dict):
            if str(obj.get('chat_name','')).strip().casefold()=='mark' and str(obj.get('chat_id','')).startswith('-'):
                candidates.add(str(obj['chat_id']))
            for val in obj.values(): walk(val)
        elif isinstance(obj,list):
            for val in obj: walk(val)
    for p in (HOME/'sessions.json',HOME/'sessions/sessions.json'):
        if p.is_file(): walk(json.loads(p.read_text()))
    check(len(candidates)==1,'mark_group_not_unique_use_group_id')
    return candidates.pop()


def token_for(primary):
    token=(((primary.get('platforms') or {}).get('telegram') or {}).get('token'))
    for line in (HOME/'.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key,value=line.removeprefix('export ').split('=',1)
            if key.strip()=='TELEGRAM_BOT_TOKEN':
                words=shlex.split(value,comments=True)
                token=words[0] if words else token
    check(bool(token),'telegram_token_missing')
    return token


def metadata(token, method, payload=None):
    try:
        request=urllib.request.Request('https://api.telegram.org/bot'+token+'/'+method,
            data=json.dumps(payload or {}).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=20) as response: result=json.load(response)
        check(result.get('ok'),'telegram_metadata_failed')
        return result['result']
    except Exception:
        raise RuntimeError('telegram_metadata_failed') from None


def apply_daily_review(owner, operator):
    """Update only MARK's scripts, instructions and owned native cron schedule."""
    global PHASE,PUBLIC_REPORT,BACKUP,SAFE_DIAGNOSTIC
    check(PROFILE.is_dir() and not (PROFILE/'gateway.parked').exists(),'profile_not_active')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    BACKUP=ACCOUNT/'migration'/('mark-daily-'+stamp)
    BACKUP.mkdir(parents=True,mode=0o700)
    PUBLIC_REPORT=EVIDENCE/('mark-biblioteca-daily-'+stamp+'.json')
    targets={'task.py':'biblioteca-task.py','runtime_check.py':'biblioteca-runtime.py',
             'SOUL.md':'SOUL.md','SKILL.md':'skills/custom/mark-biblioteca/SKILL.md'}
    for target in [*targets.values(),'cron/jobs.json','biblioteca-data/jobs.json','biblioteca-install.json']:
        source=PROFILE/target
        check(source.is_file() and not source.is_symlink(),'daily_update_file_missing')
        destination=BACKUP/target;destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        shutil.copy2(source,destination)
    PHASE='daily_update_files'
    for source,target in targets.items():
        write(PROFILE/target,(PACKAGE/source).read_bytes(),owner)
    env={'HOME':str(ACCOUNT),'HERMES_HOME':str(PROFILE),'USER':'hermes','LOGNAME':'hermes',
         'PATH':str(ACCOUNT/'.local/bin')+':/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8','TZ':'Europe/Madrid','HERMES_QUIET':'1'}
    prefix=['runuser','-u','hermes','--','env','-i',*[f'{k}={v}' for k,v in env.items()]]
    PHASE='daily_schedule'
    diagnostic=PROFILE/'biblioteca-safe-error.json';diagnostic.unlink(missing_ok=True)
    launch=json.loads(subprocess.check_output(prefix+[str(CLI),'--print-runtime-command','--module','sys'],text=True))
    check(len(launch)>=4 and launch[2]=='-c','runtime_launch')
    launch[3]=('import sys,runpy;sys.path.insert(0,'+repr(str(REPO))+');import hermes_bootstrap;'
               'sys.argv=['+repr(str(PROFILE/'biblioteca-runtime.py'))+','+repr('daily')+'];'
               'runpy.run_path('+repr(str(PROFILE/'biblioteca-runtime.py'))+',run_name="__main__")')
    with (BACKUP/'operations.log').open('ab') as log:
        result=subprocess.run(prefix+launch,stdout=log,stderr=log,timeout=120)
    if result.returncode and diagnostic.is_file():
        raw=json.loads(diagnostic.read_text())
        SAFE_DIAGNOSTIC={k:v for k,v in raw.items() if k in ('type','file','line','check')}
    check(result.returncode==0,'daily_schedule_update_failed')
    verified=json.loads((PROFILE/'biblioteca-check-daily.json').read_text())
    check(verified.get('stage')=='daily' and len(verified.get('jobs',[]))==2,'daily_report_invalid')
    setup=json.loads((PROFILE/'biblioteca-install.json').read_text())
    setup['review_cadence']='daily';write(PROFILE/'biblioteca-install.json',json.dumps(setup)+'\n',owner)
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'daily_review_configured':True,
            'jobs':verified['jobs'],'review_hour':setup['review_hour'],'report_hour':setup['report_hour'],
            'timezone':'Europe/Madrid','backup':str(BACKUP),'gateway_restarted':False,
            'renewals_triggered':False,'extra_privileges':False,'additional_service':False}
    write(PUBLIC_REPORT,json.dumps(report,ensure_ascii=False,indent=2)+'\n',operator)
    print('MARK: revisión y renovación todos los días a las '+setup['review_hour']+' Europe/Madrid.')
    print('Informe solo el domingo a las '+setup['report_hour']+'. Sin reiniciar servicios ni ejecutar renovaciones de prueba.')
    print('Informe: '+str(PUBLIC_REPORT))


def apply_accounts(core, owner, prefix):
    check((PROFILE/('.'+VERSION)).is_file(),'mark_not_installed')
    accounts=core.load_accounts(SOURCE)
    destination=PROFILE/'biblioteca-secrets/credenciales.json'
    if destination.exists():
        old={a.id:a.card for a in core.load_accounts(destination)}
        check(all(a.id not in old or old[a.id]==a.card for a in accounts),'account_id_changed_use_new_id')
    candidate=PROFILE/'biblioteca-secrets/candidate.json'
    write(candidate,SOURCE.read_bytes(),owner)
    try:
        result=subprocess.run(prefix+[str(PROFILE/'biblioteca-venv/bin/python'),str(PROFILE/'biblioteca-worker.py'),'validate-accounts'],
            capture_output=True,text=True,timeout=600)
        try: view=json.loads(result.stdout)
        except Exception: raise RuntimeError('library_read_failed') from None
        check(result.returncode==0 and view.get('accounts') and all(not a['error'] and a['loans'] is not None for a in view['accounts']),'library_read_failed')
        with core.exclusive(PROFILE/'biblioteca-data'):
            os.chown(PROFILE/'biblioteca-data/operation.lock',owner.pw_uid,owner.pw_gid)
            if destination.exists():
                stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                backup=ACCOUNT/'migration'/('mark-accounts-'+stamp)
                backup.mkdir(parents=True,mode=0o700)
                shutil.copy2(destination,backup/'credenciales.json')
            write(destination,candidate.read_bytes(),owner)
            store=core.Store(PROFILE/'biblioteca-data')
            try:
                store.sync(accounts)
                for account in accounts:
                    item=next(a for a in view['accounts'] if a['id']==account.id)
                    store.save(account,item['loans'],datetime.fromisoformat(item['checked']))
            finally: store.close()
    finally:
        candidate.unlink(missing_ok=True)


def main():
    global PHASE,PUBLIC_REPORT,BACKUP,SAFE_DIAGNOSTIC
    parser=argparse.ArgumentParser()
    parser.add_argument('--prepare',action='store_true',help='Instala aparcado, sin cuentas reales ni grupo, sin activar tareas')
    parser.add_argument('--apply-accounts',action='store_true',help='Valida y aplica el archivo de cuentas a MARK instalado, sin reiniciar ni renovar')
    parser.add_argument('--test-renewal',action='store_true',help='Tras consultar, prueba las renovaciones elegibles y vuelve a comprobar sus fechas; sin mensajes')
    parser.add_argument('--diagnose-last-failure',action='store_true',help='Lee el último log privado y publica solo categorías de error; sin instalar ni cambiar servicios')
    parser.add_argument('--daily-review',action='store_true',help='Cambia MARK ya activo a renovación diaria, conservando el informe solo dominical; no reinicia servicios')
    parser.add_argument('--group-id')
    parser.add_argument('--review-hour',default='09:00')
    parser.add_argument('--report-hour',default='09:00')
    args=parser.parse_args()
    check(os.geteuid()==0,'sudo_ordinario_required')
    os.umask(0o077)
    owner=pwd.getpwnam('hermes');operator=pwd.getpwuid(int(os.environ.get('SUDO_UID',os.getuid())))
    EVIDENCE.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chown(EVIDENCE,operator.pw_uid,operator.pw_gid)
    if args.diagnose_last_failure:
        check(not (args.prepare or args.apply_accounts or args.test_renewal or args.daily_review or args.group_id),'incompatible_modes')
        diagnose_last_failure(operator)
        return
    import grp
    check(not {'sudo','adm','docker','root'} & {grp.getgrgid(g).gr_name for g in os.getgrouplist('hermes',owner.pw_gid)},'hermes_privileges')
    for hour in (args.review_hour,args.report_hour): check(bool(re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',hour)),'hour_format')
    check(subprocess.run(['systemctl','is-active','--quiet',SERVICE]).returncode==0,'gateway_inactive')
    before=(HOME/'config.yaml').read_bytes()
    primary=yaml.safe_load(before)
    check(not PROFILE.is_symlink(),'profile_symlink')
    if PROFILE.exists(): check((PROFILE/('.'+VERSION)).is_file(),'unknown_profile')
    if args.daily_review:
        check(not (args.prepare or args.apply_accounts or args.test_renewal or args.group_id),'incompatible_modes')
        apply_daily_review(owner,operator)
        return
    spec=importlib.util.spec_from_file_location('mark_install_core',PACKAGE/'plugin/core.py')
    core=importlib.util.module_from_spec(spec);sys.modules[spec.name]=core;spec.loader.exec_module(core)
    if args.apply_accounts:
        check(not args.prepare,'incompatible_modes')
        env={'HOME':str(ACCOUNT),'HERMES_HOME':str(PROFILE),'USER':'hermes','LOGNAME':'hermes',
             'PATH':str(ACCOUNT/'.local/bin')+':/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8','TZ':'Europe/Madrid'}
        prefix=['runuser','-u','hermes','--','env','-i',*[f'{k}={v}' for k,v in env.items()]]
        apply_accounts(core,owner,prefix)
        print('Cuentas comprobadas y aplicadas sin renovar, reiniciar servicios o cambiar tareas.')
        return
    if PROFILE.exists():
        check((PROFILE/'gateway.parked').exists(),'profile_already_active_use_update_procedure')
    group=None
    if not args.prepare:
        accounts=core.load_accounts(SOURCE)  # Validates privately; no values enter stdout or reports.
        previous_accounts=PROFILE/'biblioteca-secrets/credenciales.json'
        if previous_accounts.exists():
            previous={a.id:a.card for a in core.load_accounts(previous_accounts)}
            check(all(a.id not in previous or previous[a.id]==a.card for a in accounts),'account_id_changed_use_new_id')
        group=args.group_id or find_group()
        check(bool(re.fullmatch(r'-\d+',group)),'group_id')
        token=token_for(primary)
        chat=metadata(token,'getChat',{'chat_id':group})
        check(str(chat.get('title','')).strip().casefold()=='mark' and chat['type'] in ('group','supergroup'),'group_identity')
        bot=metadata(token,'getMe')
        member=metadata(token,'getChatMember',{'chat_id':group,'user_id':bot['id']})
        check(bot.get('can_read_all_group_messages') or member.get('status')=='administrator','bot_group_privacy')
        routed_config(primary,group)  # Detect collisions before changing anything.
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup=ACCOUNT/'migration'/('mark-biblioteca-'+stamp)
    BACKUP=backup
    PUBLIC_REPORT=EVIDENCE/('mark-biblioteca-install-'+stamp+'.json')
    backup.mkdir(parents=True,mode=0o700)
    shutil.copy2(HOME/'config.yaml',backup/'config.yaml')
    voice=HOME/'gateway_voice_mode.json'
    if voice.exists(): shutil.copy2(voice,backup/voice.name)
    if PROFILE.exists():
        check((PROFILE/'gateway.parked').exists(),'profile_already_active_use_update_procedure')
        shutil.copytree(PROFILE,backup/'profile-before',ignore=shutil.ignore_patterns('biblioteca-venv','__pycache__'))
    PROFILE.mkdir(parents=True,exist_ok=True,mode=0o700)
    print('Preparando perfil MARK aparcado y entorno de navegador separado.',flush=True)
    directories=['plugins/mark-biblioteca','plugins/model-providers','skills/custom/mark-biblioteca',
                 'biblioteca-data','biblioteca-secrets','memories','workspace','sessions','scripts','cron']
    for value in ['',*directories]:
        directory=PROFILE/value
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        current=directory
        while current!=PROFILE.parent:
            check(not current.is_symlink(),'profile_directory_symlink')
            os.chown(current,owner.pw_uid,owner.pw_gid);current.chmod(0o700)
            current=current.parent
    write(PROFILE/('.'+VERSION),'',owner)
    write(PROFILE/'gateway.parked','',owner)
    write(PROFILE/'.no-bundled-skills','',owner)
    write(PROFILE/'config.yaml',yaml.safe_dump(profile_config(primary),allow_unicode=True,sort_keys=False),owner)
    write(PROFILE/'.env','TZ="Europe/Madrid"\n',owner)
    for name in ('__init__.py','core.py','web.py','plugin.yaml'):
        write(PROFILE/'plugins/mark-biblioteca'/name,(PACKAGE/'plugin'/name).read_bytes(),owner)
    for source,target in [('SOUL.md','SOUL.md'),('SKILL.md','skills/custom/mark-biblioteca/SKILL.md'),
                          ('worker.py','biblioteca-worker.py'),('runtime_check.py','biblioteca-runtime.py'),('task.py','biblioteca-task.py')]:
        write(PROFILE/target,(PACKAGE/source).read_bytes(),owner)
    for mode in ('review','sunday'):
        body=f"import runpy,sys\nfrom pathlib import Path\nsys.argv=['biblioteca-task.py',{mode!r}]\nrunpy.run_path(str(Path(__file__).resolve().parent.parent/'biblioteca-task.py'),run_name='__main__')\n"
        write(PROFILE/f'scripts/mark-{mode}.py',body,owner)
    setup={'group_id':group,'review_hour':args.review_hour,'report_hour':args.report_hour,'owner':VERSION,'review_cadence':'daily'}
    write(PROFILE/'biblioteca-install.json',json.dumps(setup)+'\n',owner)
    env={'HOME':str(ACCOUNT),'HERMES_HOME':str(PROFILE),'USER':'hermes','LOGNAME':'hermes',
         'PATH':str(ACCOUNT/'.local/bin')+':/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8','TZ':'Europe/Madrid','HERMES_QUIET':'1'}
    prefix=['runuser','-u','hermes','--','env','-i',*[f'{k}={v}' for k,v in env.items()]]
    logpath=backup/'operations.log'
    def command(argv,timeout=600):
        global SAFE_DIAGNOSTIC
        with logpath.open('ab') as log:
            offset=log.tell()
            result=subprocess.run(argv,stdout=log,stderr=log,timeout=timeout)
        if result.returncode:
            # Publish only fixed categories. Never expose log text, URLs or argv:
            # package managers and upstream errors can contain private values.
            with logpath.open('rb') as log:
                log.seek(max(offset,logpath.stat().st_size-65536))
                diagnostic_text=log.read().decode(errors='replace').lower()
            SAFE_DIAGNOSTIC={'operation':PHASE,'exit_code':result.returncode,**safe_failure_details(diagnostic_text)}
        check(result.returncode==0,'private_operation_failed')
    def runtime(stage):
        global PHASE,SAFE_DIAGNOSTIC
        PHASE='native_'+stage
        diagnostic=PROFILE/'biblioteca-safe-error.json'
        diagnostic.unlink(missing_ok=True)
        launch=json.loads(subprocess.check_output(prefix+[str(CLI),'--print-runtime-command','--module','sys'],text=True))
        check(len(launch)>=4 and launch[2]=='-c','runtime_launch')
        launch[3]=('import sys,runpy;sys.path.insert(0,'+repr(str(REPO))+');import hermes_bootstrap;'
                   'sys.argv=['+repr(str(PROFILE/'biblioteca-runtime.py'))+','+repr(stage)+'];'
                   'runpy.run_path('+repr(str(PROFILE/'biblioteca-runtime.py'))+',run_name="__main__")')
        try:
            command(prefix+launch,240)
        except Exception:
            if diagnostic.is_file():
                value=json.loads(diagnostic.read_text())
                SAFE_DIAGNOSTIC={k:v for k,v in value.items() if k in ('type','file','line','check')}
            raise
    venv=PROFILE/'biblioteca-venv'
    PHASE='browser_venv'
    print('Comprobando Playwright, Chromium y sus bibliotecas de sistema.',flush=True)
    if not (venv/'bin/python').is_file(): command(prefix+[sys.executable,'-m','venv',str(venv)])
    PHASE='browser_python_packages'
    command(prefix+[str(venv/'bin/python'),'-m','pip','install','playwright==1.64.0'])
    PHASE='browser_download'
    command(prefix+[str(venv/'bin/python'),'-m','playwright','install','chromium','--only-shell'],900)
    # Existing QA used extracted libraries; production needs its own supported system libs.
    shell_binaries=list((ACCOUNT/'.cache/ms-playwright').glob('chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell'))
    packages=missing_browser_packages(shell_binaries)
    missing_system_libraries=bool(packages)
    if missing_system_libraries:
        PHASE='browser_system_libraries'
        # Use the distro's package manager directly, with the already available
        # indexes. The broad Playwright installer also installs Xvfb/font stacks
        # and runs a global apt update before reaching the needed libraries.
        command(['env','DEBIAN_FRONTEND=noninteractive','apt-get','install','-y','--no-install-recommends',*packages],900)
        check(not missing_browser_packages(shell_binaries),'browser_libraries_still_missing')
    PHASE='browser_probe'
    probe='from playwright.sync_api import sync_playwright\np=sync_playwright().start();b=p.chromium.launch(headless=True);s=b.new_page();s.set_content("<h1>Prueba</h1>");assert s.title()=="";b.close();p.stop()'
    command(prefix+[str(venv/'bin/python'),'-c',probe])
    runtime('verify');runtime('prepare')
    if not args.prepare:
        PHASE='library_read'
        print('Consultando las cuentas reales sin renovar; los listados permanecen privados.',flush=True)
        write(PROFILE/'biblioteca-secrets/credenciales.json',SOURCE.read_bytes(),owner)
        # Validate real account reads before exposing MARK or enabling renewals.
        with logpath.open('ab') as log:
            result=subprocess.run(prefix+[str(venv/'bin/python'),str(PROFILE/'biblioteca-worker.py'),'read'],stdout=subprocess.PIPE,stderr=log,text=True,timeout=600)
        try: view=json.loads(result.stdout)
        except Exception: raise RuntimeError('library_read_failed') from None
        check(result.returncode==0 and view.get('accounts') and all(not a['error'] and a['loans'] is not None for a in view['accounts']),'library_read_failed')
        print('Comprobando el modelo y la herramienta de estado, con renovación bloqueada en esta prueba.',flush=True)
        runtime('model')
        eligible=sum(1 for a in view['accounts'] for loan in a['loans'] if loan['renewable'])
        renewal_verified=False
        if args.test_renewal:
            PHASE='renewal_test'
            print('Probando préstamos elegibles y verificando de nuevo sus fechas, sin mensajes Telegram.',flush=True)
            old={(a['id'],loan['id']):loan['due'] for a in view['accounts'] for loan in a['loans'] if loan['renewable']}
            with logpath.open('ab') as log:
                renewed=subprocess.run(prefix+[str(venv/'bin/python'),str(PROFILE/'biblioteca-worker.py'),'renew'],stdout=subprocess.PIPE,stderr=log,text=True,timeout=600)
                reread=subprocess.run(prefix+[str(venv/'bin/python'),str(PROFILE/'biblioteca-worker.py'),'read'],stdout=subprocess.PIPE,stderr=log,text=True,timeout=600)
            try:
                after=json.loads(reread.stdout);renew_result=json.loads(renewed.stdout)
                check(renewed.returncode==0 and reread.returncode==0 and 'accounts' in after and 'accounts' in renew_result,'renewal_test_failed')
                check(all(not a['error'] for a in after['accounts']),'renewal_test_unconfirmed')
                changed=[loan for a in after['accounts'] for loan in a['loans']
                         if (a['id'],loan['id']) in old and core.parse_date(loan['due'])>core.parse_date(old[(a['id'],loan['id'])])]
                renewal_verified=bool(changed)
            except Exception:
                raise RuntimeError('renewal_test_unconfirmed') from None
    activated=False
    config_written=False
    try:
        if not args.prepare:
            PHASE='activate_config'
            print('Activando la ruta de MARK y las tareas a las horas configuradas.',flush=True)
            check((HOME/'config.yaml').read_bytes()==before,'concurrent_config_change')
            write(HOME/'config.yaml',yaml.safe_dump(routed_config(primary,group),allow_unicode=True,sort_keys=False),owner)
            config_written=True
            modes=json.loads(voice.read_text()) if voice.exists() else {}
            modes['telegram:'+group]='off';write(voice,json.dumps(modes)+'\n',owner)
            cfg=yaml.safe_load((PROFILE/'config.yaml').read_text());cfg['gateway'].pop('parked',None)
            write(PROFILE/'config.yaml',yaml.safe_dump(cfg,allow_unicode=True,sort_keys=False),owner)
            (PROFILE/'gateway.parked').unlink()
            runtime('routing')
            PHASE='gateway_restart'
            subprocess.run(['systemctl','restart',SERVICE],check=True,timeout=90)
            for _ in range(45):
                state=json.loads((HOME/'gateway_state.json').read_text())
                pid=subprocess.check_output(['systemctl','show',SERVICE,'-p','MainPID','--value'],text=True).strip()
                if str(state.get('pid'))==pid and 'mark' in state.get('served_profiles',[]) and state.get('platforms',{}).get('telegram',{}).get('state')=='connected':
                    break
                time.sleep(1)
            else: raise RuntimeError('gateway_mark_not_ready')
            runtime('activate')
            activated=True
    finally:
        if config_written and not activated:
            failed_phase,failed_diagnostic=PHASE,SAFE_DIAGNOSTIC
            try:
                try: runtime('pause')
                except Exception: pass
                write(HOME/'config.yaml',before,owner)
                if (backup/voice.name).exists(): write(voice,(backup/voice.name).read_bytes(),owner)
                else: voice.unlink(missing_ok=True)
                write(PROFILE/'gateway.parked','',owner)
                subprocess.run(['systemctl','restart',SERVICE],timeout=90,check=True)
                check(subprocess.run(['systemctl','is-active','--quiet',SERVICE]).returncode==0,'rollback_gateway_failed')
            finally:
                PHASE,SAFE_DIAGNOSTIC=failed_phase,failed_diagnostic
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'prepared':True,'installed':True,
            'activated':activated,'library_read_verified':not args.prepare,
            'renewal_live_verified':renewal_verified if not args.prepare else False,
            'eligible_loans_at_test':eligible if not args.prepare else None,
            'renewal_test_requested':args.test_renewal,'mobile_acceptance_pending':True,'additional_service':False,
            'extra_privileges':False,'backup':str(backup),'review_hour':args.review_hour,'report_hour':args.report_hour}
    report['browser_system_dependencies_requested']=missing_system_libraries
    report['browser_system_packages_requested']=packages
    for stage in ('verify','prepare','model','activate','routing'):
        path=PROFILE/f'biblioteca-check-{stage}.json'
        if path.is_file(): report[stage]=json.loads(path.read_text())
    public=PUBLIC_REPORT
    write(public,json.dumps(report,ensure_ascii=False,indent=2)+'\n',operator)
    print('MARK preparado y aparcado.' if args.prepare else 'MARK instalado; consulta de cuentas correcta y tareas activas. Falta aceptación desde Telegram.')
    if not args.prepare and not renewal_verified:
        print('La renovación real no está acreditada todavía; observar el primer préstamo elegible.')
    print('Informe: '+str(public))


if __name__=='__main__':
    try: main()
    except Exception as exc:
        # Print only own closed codes, never secret-bearing upstream messages.
        code=str(exc) if type(exc).__name__ in ('RuntimeError','LibraryError') and re.fullmatch('[a-z_]+',str(exc)) else type(exc).__name__
        if PUBLIC_REPORT is not None:
            try:
                operator=pwd.getpwuid(int(os.environ.get('SUDO_UID',os.getuid())))
                write(PUBLIC_REPORT,json.dumps({'checked_at':datetime.now(timezone.utc).isoformat(),
                    'activated':False,'phase':PHASE,'error_code':code,'backup':str(BACKUP),
                    'requires_verification':True,'safe_diagnostic':SAFE_DIAGNOSTIC})+'\n',operator)
            except Exception: pass
        print('MARK: '+code+'. No se afirma activación completa. Revisar informe privado de instalación.',file=sys.stderr)
        raise SystemExit(1)
