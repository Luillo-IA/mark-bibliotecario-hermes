"""Cron determinista. Ambos jobs entregan solo local; Telegram tiene ventana dominical."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import urllib.request


def load_plugin(profile):
    spec=importlib.util.spec_from_file_location('mark_library_runtime',profile/'plugins/mark-biblioteca/__init__.py',
                                              submodule_search_locations=[str(profile/'plugins/mark-biblioteca')])
    mod=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=mod
    spec.loader.exec_module(mod)
    return mod


def existing_bot(profile):
    # Read only the existing receiver's Telegram token; no secret is copied into MARK.
    from gateway import config_loader
    from gateway.config import GatewayConfig, Platform
    primary=profile.parent.parent
    raw={}
    config_loader.load_yaml_layer(primary,raw)
    gateway=GatewayConfig.from_dict(raw)
    routes=[r for r in gateway.profile_routes if r.enabled and r.platform=='telegram' and r.profile=='mark' and r.chat_id]
    if len(routes)!=1: raise RuntimeError('route_scope')
    target=routes[0].chat_id
    platform=gateway.platforms.get(Platform.TELEGRAM)
    token=platform.token if platform else None
    # Support env-backed bot credentials without exporting the entire primary environment.
    for line in (primary/'.env').read_text().splitlines():
        if line.strip().startswith('#') or '=' not in line: continue
        key,value=line.removeprefix('export ').split('=',1)
        if key.strip()=='TELEGRAM_BOT_TOKEN':
            words=shlex.split(value,comments=True)
            token=words[0] if words else token
    if not token: raise RuntimeError('telegram_token_missing')
    def send(text):
        request=urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',
            data=json.dumps({'chat_id':target,'text':text,'link_preview_options':{'is_disabled':True}}).encode(),
            headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=20) as response:
                result=json.load(response)
            if not result.get('ok'): raise RuntimeError()
        except Exception:
            raise RuntimeError('delivery_unknown') from None
    return send


def execute(profile, mode, *, now=None, backend=None, sender=None, clock_fn=None):
    plugin=load_plugin(profile)
    from mark_library_runtime import core
    now=now or (clock_fn or core.clock)()
    if mode not in ('review','sunday'): raise core.LibraryError('invalid_mode')
    # On Sunday the report task performs the daily renewal before composing the
    # report. Keeping one browser task per day avoids conflicting renewals at 09:00.
    if mode=='review' and now.weekday()==6:
        return {'status':'outside_review_days'}
    if mode=='sunday' and now.weekday()!=6:
        return {'status':'outside_sunday'}
    try:
        accounts=core.load_accounts(profile/'biblioteca-secrets/credenciales.json')
        if backend is None:
            view=plugin.run_worker(profile,renew=True)
        else:
            view=core.run(profile/'biblioteca-data',accounts,backend,renew=True,now=now)
    except Exception:
        if mode!='sunday': raise
        store=core.Store(profile/'biblioteca-data')
        try: view=store.view()
        finally: store.close()
        for account in view['accounts']: account['error']='refresh_failed'
        view['refresh_error']=True
    if mode=='sunday':
        message=core.summary(view,now)
        if view.get('refresh_error'): message+='\n⚠️ No se ha podido actualizar el portal; revisar manualmente.'
        result=core.sunday_delivery(profile/'biblioteca-data',message,
                                    sender or existing_bot(profile),now_fn=clock_fn or core.clock)
        return {'status':result['status'],'sent_parts':result['sent_parts']}
    return {'status':view['last_run']['status']}


def main():
    from hermes_cli.config import get_hermes_home
    profile=get_hermes_home().resolve()
    if profile.name!='mark' or profile.parent.name!='profiles':
        return 1
    mode=sys.argv[1] if len(sys.argv)==2 else ''
    try:
        result=execute(profile,mode)
    except Exception:
        # No external exception, secret, HTML or message contents on stdout/stderr.
        result={'status':'operation_failed'}
    path=profile/'biblioteca-data'/('task-'+('sunday' if mode=='sunday' else 'review')+'.json')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.write_text(json.dumps(result)+'\n')
    path.chmod(0o600)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
