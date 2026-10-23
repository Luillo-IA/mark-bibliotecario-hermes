"""Comprobaciones nativas de MARK. Solo métricas en el informe público."""
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


def require(value, code):
    if not value: raise RuntimeError(code)


def jobs_setup(home, stage):
    from cron.jobs import create_job, list_jobs, pause_job, resume_job, update_job, parse_schedule, compute_next_run
    setup=json.loads((home/'biblioteca-install.json').read_text())
    statepath=home/'biblioteca-data/jobs.json'
    state=json.loads(statepath.read_text()) if statepath.exists() else {}
    # Sunday renews in the report task, so the two jobs never compete for the browser.
    specs=[('review','MARK biblioteca — revisión silenciosa','1-6',setup['review_hour']),
           ('sunday','MARK biblioteca — informe dominical','0',setup['report_hour'])]
    plan=[]
    for key,name,days,hour in specs:
        minute,hour_value=hour.split(':')[1],hour.split(':')[0]
        schedule=f'{int(minute)} {int(hour_value)} * * {days}'
        found=[j for j in list_jobs(include_disabled=True) if j.get('name')==name]
        require(len(found)<=1,'duplicate_job')
        if found:
            job=found[0]
            require(state.get(key)==job['id'],'job_ownership')
            legacy=f'{int(minute)} {int(hour_value)} * * 1,4,6'
            require(job['schedule']==parse_schedule(schedule) or
                    stage=='daily' and key=='review' and job['schedule']==parse_schedule(legacy),'schedule_changed')
            require(job.get('script')==f'mark-{key}.py' and job.get('no_agent') is True,'job_payload')
            require(job.get('deliver')=='local' and job.get('failure_deliver')=='local','delivery_silence')
            if stage=='daily': require(job.get('enabled') is True,'job_paused')
        else:
            require(stage=='prepare','job_missing')
            job=create_job(prompt=None,schedule=schedule,name=name,deliver='local',failure_deliver='local',
                           no_agent=True,script=f'mark-{key}.py',enabled_toolsets=[],
                           attach_to_session=False,paused=True,paused_reason='Validación de MARK pendiente')
            state[key]=job['id']
            statepath.write_text(json.dumps(state)+'\n');statepath.chmod(0o600)
        # Test the chosen local time across both sides of the October DST transition.
        zone=ZoneInfo('Europe/Madrid')
        for previous in ('2026-10-18T23:00:00+02:00','2026-10-25T23:00:00+01:00'):
            upcoming=datetime.fromisoformat(compute_next_run(parse_schedule(schedule),last_run_at=previous)).astimezone(zone)
            require(upcoming.weekday() in (range(6) if key=='review' else (6,)),'weekday')
            require((upcoming.hour,upcoming.minute)==(int(hour_value),int(minute)),'dst_hour')
        plan.append((key,job,schedule))
    # Validate ownership and both payloads before changing an existing schedule.
    result=[]
    for key,job,schedule in plan:
        if stage=='daily' and key=='review' and job['schedule']!=parse_schedule(schedule):
            job=update_job(job['id'],{'schedule':schedule})
            require(job is not None,'schedule_update_failed')
        if stage=='activate': job=resume_job(job['id'])
        elif stage=='pause': job=pause_job(job['id'],reason='Instalación no confirmada; sin ejecución')
        result.append({'task':key,'schedule':job['schedule'],'enabled':job['enabled'],'next_run_at':job.get('next_run_at'),
                       'no_agent':job['no_agent'],'deliver':'local','failure_deliver':'local'})
    return result


def verify(home):
    import model_tools
    from hermes_cli.plugins import discover_plugins
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import get_tool_definitions
    from tools.registry import registry
    discover_plugins()
    cfg=load_config()
    tools=get_tool_definitions(enabled_toolsets=list(_get_platform_tools(cfg,'telegram')),
        disabled_toolsets=cfg['agent']['disabled_toolsets'],quiet_mode=True,skip_tool_search_assembly=True)
    names={t.get('function',t).get('name') for t in tools}
    require(names=={'mark_biblioteca','memory','text_to_speech'},'effective_tools')
    result=json.loads(registry.get_entry('mark_biblioteca').handler({'action':'estado'}))
    require('accounts' in result and 'error' not in result,'tool_state_read')
    from agent.secret_scope import set_multiplex_active
    from gateway.run import _profile_runtime_scope
    from hermes_cli.model_switch_providers import list_authenticated_providers
    from hermes_cli.runtime_provider import resolve_runtime_provider
    set_multiplex_active(True)
    with _profile_runtime_scope(home):
        model=cfg['model']
        rows=list_authenticated_providers(current_provider=model.get('provider'),current_model=model.get('default'),
            user_providers=cfg.get('providers'),custom_providers=cfg.get('custom_providers'),
            for_picker=True,excluded_providers=cfg.get('model_catalog',{}).get('excluded_providers'),
            non_blocking_catalogs=True,probe_custom_providers=False)
        slugs={r['slug'] for r in rows}
        resolved=resolve_runtime_provider(requested=model['provider'],target_model=model['default'])
        require(bool(resolved.get('api_key')),'default_provider_key')
    return {'effective_tools':sorted(names),'provider_picker':sorted(slugs),'default_provider_resolved':True}


def probe_model(home):
    from gateway.run import _profile_runtime_scope
    with _profile_runtime_scope(home):
        verify(home)
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from run_agent import AIAgent
        cfg=load_config();model=cfg['model']
        runtime=resolve_runtime_provider(requested=model['provider'],target_model=model['default'])
        executed=[]
        from tools.registry import registry
        entry=registry.get_entry('mark_biblioteca')
        original_handler=entry.handler
        def state_only(args,**kwargs):
            if args!={'action':'estado'}:
                return json.dumps({'error':'read_only_probe'})
            return original_handler(args,**kwargs)
        entry.handler=state_only
        def complete(call_id,name,tool_args,output,**kwargs):
            try:
                if isinstance(tool_args,str): tool_args=json.loads(tool_args)
                value=json.loads(output) if isinstance(output,str) else output
                if name=='mark_biblioteca' and tool_args.get('action')=='estado' and 'accounts' in value:
                    executed.append(name)
            except Exception: pass
        agent=AIAgent(model=model['default'],provider=runtime['provider'],api_key=runtime['api_key'],
            base_url=runtime['base_url'],api_mode=runtime.get('api_mode'),
            enabled_toolsets=['mark_biblioteca'],quiet_mode=True,max_iterations=4,max_tokens=2048,
            tool_complete_callback=complete,save_trajectories=False,skip_background_review=True,
            skip_memory=True,skip_context_files=True,
            ephemeral_system_prompt='Prueba de biblioteca de MARK. Solo lee el estado ya guardado. No consultes el portal ni renueves.')
        try:
            answer=agent.run_conversation('Usa mark_biblioteca con action=estado y confirma brevemente si tienes un listado guardado. No consultes ni renueves.')
        finally:
            entry.handler=original_handler
        require(executed and not answer.get('error') and not answer.get('failed') and answer.get('completed') is not False,'model_tool_cycle')
        return {'inference_verified':True,'state_tool_cycle_verified':True,'renewal_requested_by_model':False}


def main():
    from hermes_cli.config import get_hermes_home
    home=get_hermes_home().resolve()
    require(home.name=='mark' and home.parent.name=='profiles','profile_scope')
    stage=sys.argv[1]
    if stage=='verify': result=verify(home)
    elif stage=='model': result=probe_model(home)
    elif stage in ('prepare','activate','pause','daily'): result={'jobs':jobs_setup(home,stage)}
    elif stage=='routing':
        from gateway import config_loader
        from gateway.config import GatewayConfig, Platform
        raw={};config_loader.load_yaml_layer(home.parent.parent,raw)
        cfg=GatewayConfig.from_dict(raw)
        routes=[r for r in cfg.profile_routes if r.platform=='telegram' and r.profile=='mark']
        expected=json.loads((home/'biblioteca-install.json').read_text())['group_id']
        require(len(routes)==1 and str(routes[0].chat_id)==expected,'native_route')
        platform=cfg.platforms[Platform.TELEGRAM]
        require(expected in [str(v) for v in platform.extra.get('group_allowed_chats',[])],'group_authorized')
        require(expected in [str(v) for v in platform.extra.get('free_response_chats',[])],'group_free_response')
        result={'native_route_verified':True}
    else: raise RuntimeError('invalid_stage')
    result.update(stage=stage,checked_at=datetime.now(ZoneInfo('Europe/Madrid')).isoformat())
    p=home/f'biblioteca-check-{stage}.json'
    p.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');p.chmod(0o600)


if __name__=='__main__':
    try: main()
    except Exception as exc:
        # No secret-bearing traceback. Full code + check position is sufficient to diagnose.
        import traceback
        from hermes_cli.config import get_hermes_home
        last=traceback.extract_tb(exc.__traceback__)[-1]
        (get_hermes_home()/'biblioteca-safe-error.json').write_text(json.dumps({
            'type':type(exc).__name__,'file':Path(last.filename).name,'line':last.lineno,
            'check':str(exc) if type(exc) is RuntimeError and len(str(exc))<60 and str(exc).replace('_','').isalnum() else 'native_check'}))
        raise SystemExit(1)
