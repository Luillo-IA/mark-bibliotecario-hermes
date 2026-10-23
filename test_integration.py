"""Cron/configuración nativos en HOME temporal y publicación simulada, sin datos reales."""
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
REPO=Path(os.environ.get('HERMES_SOURCE','/nonexistent/hermes-source'))
DEPS=Path(os.environ.get('HERMES_TEST_DEPS',str(REPO)))


def load(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec);sys.modules[name]=mod;spec.loader.exec_module(mod)
    return mod


class ConfigurationTests(unittest.TestCase):
    def setUp(self): self.install=load('mark_install_test',HERE/'install.py')
    def test_route_changes_only_mark_and_preserves_all_existing_data(self):
        primary={'model':{'provider':'deepseek','default':'model'},'stt':{'provider':'local'},
                 'profile_routes':[{'name':'otro','platform':'telegram','chat_id':'-1','profile':'otro'}],
                 'platforms':{'telegram':{'extra':{'allow_from':['OWNER'],'free_response_chats':['-1']}}}}
        changed=self.install.routed_config(primary,'-2')
        self.assertEqual(changed['profile_routes'][0],primary['profile_routes'][0])
        self.assertEqual(changed['model'],primary['model'])
        self.assertEqual(changed['stt'],primary['stt'])
        self.assertEqual(changed['platforms']['telegram']['extra']['allow_from'],['OWNER'])
        self.assertEqual(changed['platforms']['telegram']['extra']['group_allowed_chats'],['-2'])
        self.assertNotIn('group_allowed_chats',primary['platforms']['telegram']['extra'])
        self.assertEqual(self.install.routed_config(changed,'-2'),changed)
    def test_cannot_hijack_another_group_or_mark_route(self):
        with self.assertRaisesRegex(RuntimeError,'group_collision'):
            self.install.routed_config({'profile_routes':[{'platform':'telegram','chat_id':'-2','profile':'otro'}]},'-2')
        with self.assertRaisesRegex(RuntimeError,'profile_collision'):
            self.install.routed_config({'profile_routes':[{'platform':'telegram','chat_id':'-3','profile':'mark'}]},'-2')
    def test_profile_never_clones_general_tools_or_bot_token(self):
        result=self.install.profile_config({'model':{'provider':'deepseek','default':'model'},
            'platform_toolsets':{'telegram':['terminal','file','spotify']},'platforms':{'telegram':{'token':'SECRET'}},
            'stt':{'model':'small'},'tts':{'provider':'piper'}})
        self.assertEqual(result['platform_toolsets']['telegram'],['mark_biblioteca','memory','tts'])
        self.assertEqual(result['stt'],{'model':'small'})
        self.assertNotIn('SECRET',json.dumps(result))
        self.assertNotIn('platforms',result)
        self.assertTrue(result['gateway']['parked'])


@unittest.skipUnless((REPO/'hermes_cli').is_dir(),'Set HERMES_SOURCE to an installed Hermes source checkout')
class NativeTests(unittest.TestCase):
    def run_native(self, code):
        with tempfile.TemporaryDirectory(prefix='mark-native-') as folder:
            env={**os.environ,'HERMES_HOME':folder,'HOME':folder,'HERMES_TIMEZONE':'Europe/Madrid',
                 'TZ':'Europe/Madrid','PYTHONPATH':os.pathsep.join([str(REPO),str(DEPS),os.environ.get('HERMES_EXTRA_TEST_DEPS','')])}
            result=subprocess.run([sys.executable,'-c',code,str(HERE)],env=env,capture_output=True,text=True,timeout=50)
            self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
    def test_native_two_jobs_roundtrip_dst_pause_and_no_model(self):
        self.run_native('''
import importlib.util,sys,json,os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
spec=importlib.util.spec_from_file_location('mark_setup',Path(sys.argv[1])/'runtime_check.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
home=Path(os.environ['HERMES_HOME'])
(home/'biblioteca-data').mkdir()
(home/'biblioteca-install.json').write_text(json.dumps({'review_hour':'09:00','report_hour':'20:00'}))
first=m.jobs_setup(home,'prepare')
second=m.jobs_setup(home,'prepare')
from cron.jobs import list_jobs,get_job
assert len(list_jobs(include_disabled=True))==2
assert first==second
result=m.jobs_setup(home,'activate')
assert all(j['enabled'] and j['no_agent'] and j['deliver']=='local' and j['failure_deliver']=='local' for j in result)
for job in list_jobs(include_disabled=True):
 assert job['enabled_toolsets']==[] and job['attach_to_session'] is False
 assert job['no_agent'] is True and job['script'] in ('mark-review.py','mark-sunday.py')
upcoming={j['task']:datetime.fromisoformat(j['next_run_at']).astimezone(ZoneInfo('Europe/Madrid')) for j in result}
assert upcoming['review'].weekday() in range(6) and upcoming['review'].hour==9
assert upcoming['sunday'].weekday()==6 and upcoming['sunday'].hour==20
assert all(not j['enabled'] for j in m.jobs_setup(home,'pause'))
''')
    def test_native_daily_migration_preserves_sunday_and_other_jobs(self):
        self.run_native('''
import importlib.util,sys,json,os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
spec=importlib.util.spec_from_file_location('mark_setup',Path(sys.argv[1])/'runtime_check.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
home=Path(os.environ['HERMES_HOME']);(home/'biblioteca-data').mkdir()
(home/'biblioteca-install.json').write_text(json.dumps({'review_hour':'09:00','report_hour':'09:00'}))
m.jobs_setup(home,'prepare');m.jobs_setup(home,'activate')
from cron.jobs import get_job,update_job,create_job,parse_schedule,compute_next_run,list_jobs
state=json.loads((home/'biblioteca-data/jobs.json').read_text())
update_job(state['review'],{'schedule':'0 9 * * 1,4,6'})
sunday=get_job(state['sunday'])
other=create_job(prompt='Ficticio',schedule='0 15 * * *',name='Otra tarea',paused=True)
first=m.jobs_setup(home,'daily');second=m.jobs_setup(home,'daily')
assert first==second
assert get_job(state['sunday'])==sunday and get_job(other['id'])==other
assert len(list_jobs(include_disabled=True))==3
review=get_job(state['review']);assert review['schedule']==parse_schedule('0 9 * * 1-6')
zone=ZoneInfo('Europe/Madrid')
for previous in ('2026-10-22T12:00:00+02:00','2026-10-25T12:00:00+01:00'):
 nxt=datetime.fromisoformat(compute_next_run(review['schedule'],last_run_at=previous)).astimezone(zone)
 assert nxt.hour==9 and nxt.weekday() in range(6)
assert datetime.fromisoformat(compute_next_run(review['schedule'],last_run_at='2026-10-22T12:00:00+02:00')).astimezone(zone).weekday()==4
# Refuse unexpected changes before mutating either job.
update_job(state['review'],{'schedule':'0 9 * * 1,4,6'})
update_job(state['sunday'],{'schedule':'0 10 * * 0'})
try:m.jobs_setup(home,'daily')
except RuntimeError as exc:assert str(exc)=='schedule_changed'
else:raise AssertionError('unexpected Sunday schedule accepted')
assert get_job(state['review'])['schedule']==parse_schedule('0 9 * * 1,4,6')
''')
    def test_native_plugin_discovery_and_handler_no_arbitrary_tools(self):
        self.run_native('''
import sys,os,shutil,json
from pathlib import Path
home=Path(os.environ['HERMES_HOME'])/'profiles/mark'
home.mkdir(parents=True)
os.environ['HERMES_HOME']=str(home)
shutil.copytree(Path(sys.argv[1])/'plugin',home/'plugins/mark-biblioteca')
(home/'config.yaml').write_text('plugins:\\n  enabled: [mark-biblioteca]\\n')
from hermes_cli.plugins import discover_plugins
from tools.registry import registry
discover_plugins()
entry=registry.get_entry('mark_biblioteca')
assert entry is not None
result=json.loads(entry.handler({'action':'estado'}))
assert result['accounts']==[] and 'error' not in result
assert json.loads(entry.handler({'action':'consultar'}))['source']=='local_table'
assert json.loads(entry.handler({'action':'renovar'}))['error']=='invalid_action'
assert 'error' in json.loads(entry.handler({'action':'estado','path':'/etc/passwd'}))
''')
    def test_native_group_gate_keeps_miembro_out_of_dm(self):
        self.run_native('''
import sys,importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('mark_install',Path(sys.argv[1])/'install.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from gateway.config import GatewayConfig,Platform
from gateway.config_loader import bridge_toplevel_keys,merge_platform_sections
raw=m.routed_config({'platforms':{'telegram':{'enabled':True,'extra':{'allow_from':['OWNER']}}}},'-2')
data={};bridge_toplevel_keys(raw,raw['gateway'],data)
data['platforms']=merge_platform_sections(raw,None,data)
cfg=GatewayConfig.from_dict(data)
extra=cfg.platforms[Platform.TELEGRAM].extra
assert extra['group_allowed_chats']==['-2']
assert extra['allow_from']==['OWNER']
assert len(cfg.profile_routes)==1 and cfg.profile_routes[0].profile=='mark'
from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.session import SessionSource
from types import SimpleNamespace
from unittest.mock import patch
class Gate(GatewayAuthorizationMixin):
 def _adapter_profile_for_source(self,source): return 'default'
 def _delivery_adapter_for(self,source): return SimpleNamespace(config=cfg.platforms[Platform.TELEGRAM])
 def _adapter_flag(self,*a): return False
g=Gate()
with patch('gateway.authz_mixin._auth_env',return_value=''):
 assert g._is_user_authorized(SessionSource(platform=Platform.TELEGRAM,chat_id='-2',chat_type='group',user_id='MEMBER'))
 assert not g._is_user_authorized(SessionSource(platform=Platform.TELEGRAM,chat_id='MEMBER',chat_type='dm',user_id='MEMBER'))
 assert not g._is_user_authorized(SessionSource(platform=Platform.TELEGRAM,chat_id='-3',chat_type='group',user_id='MEMBER'))
 assert g._is_user_authorized(SessionSource(platform=Platform.TELEGRAM,chat_id='OWNER',chat_type='dm',user_id='OWNER'))
''')
    def test_model_probe_has_technical_read_only_guard(self):
        self.run_native('''
import os,sys,json,shutil,importlib.util,types
from pathlib import Path
home=Path(os.environ['HERMES_HOME'])/'profiles/mark';home.mkdir(parents=True)
os.environ['HERMES_HOME']=str(home)
shutil.copytree(Path(sys.argv[1])/'plugin',home/'plugins/mark-biblioteca')
(home/'config.yaml').write_text('plugins:\\n  enabled: [mark-biblioteca]\\nmodel:\\n  provider: deepseek\\n  default: ficticio\\n')
from hermes_cli.plugins import discover_plugins
from tools.registry import registry
discover_plugins()
original=registry.get_entry('mark_biblioteca').handler
from contextlib import contextmanager
scope={'active':False,'calls':0}
@contextmanager
def profile_scope(path):
 assert path==home
 scope['active']=True;scope['calls']+=1
 try:yield
 finally:scope['active']=False
class FakeAgent:
 def __init__(self,**kwargs):
  assert scope['active']
  self.callback=kwargs['tool_complete_callback']
 def run_conversation(self,prompt):
  assert scope['active']
  handler=registry.get_entry('mark_biblioteca').handler
  assert json.loads(handler({'action':'renovar'}))['error']=='read_only_probe'
  args={'action':'estado'};output=handler(args)
  self.callback('id','mark_biblioteca',args,output)
  return {'completed':True,'failed':False}
sys.modules['run_agent']=types.SimpleNamespace(AIAgent=FakeAgent)
sys.modules['gateway.run']=types.SimpleNamespace(_profile_runtime_scope=profile_scope)
def resolve(**kwargs):
 assert scope['active']
 return {'provider':'deepseek','api_key':'FAKE','base_url':'https://example.invalid','api_mode':'chat_completions'}
sys.modules['hermes_cli.runtime_provider']=types.SimpleNamespace(resolve_runtime_provider=resolve)
spec=importlib.util.spec_from_file_location('mark_check',Path(sys.argv[1])/'runtime_check.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.verify=lambda home:None
sys.argv=['check.py','model'];m.main()
assert registry.get_entry('mark_biblioteca').handler is original
assert scope['calls']==1 and not scope['active']
assert json.loads((home/'biblioteca-check-model.json').read_text())['state_tool_cycle_verified'] is True
''')


if __name__=='__main__': unittest.main(verbosity=2)
