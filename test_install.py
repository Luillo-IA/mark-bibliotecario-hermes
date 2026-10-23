"""Installer orchestration on a fake server; no sudo, library, services or Telegram."""
from contextlib import ExitStack, redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_integration import load, HERE


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.install=load('mark_install_orchestration',HERE/'install.py')
        m=self.install
        m.ACCOUNT=self.root/'hermes';m.HOME=m.ACCOUNT/'.hermes';m.PROFILE=m.HOME/'profiles/mark'
        m.REPO=m.HOME/'hermes-agent';m.CLI=m.ACCOUNT/'bin/hermes'
        m.SOURCE=self.root/'private/credenciales.json'
        m.EVIDENCE=self.root/'evidence';m.EVIDENCE.mkdir()
        m.HOME.mkdir(parents=True)
        original={'model':{'provider':'deepseek','default':'example'},'profile_routes':[
            {'name':'otro','profile':'otro','platform':'telegram','chat_id':'-1'}]}
        (m.HOME/'config.yaml').write_text(__import__('yaml').safe_dump(original))
        self.original=(m.HOME/'config.yaml').read_bytes()
        (m.HOME/'.env').write_text('TELEGRAM_BOT_TOKEN=FAKE_TOKEN\n')
        provider=m.HOME/'plugins/provider/antigravity-oauth';provider.mkdir(parents=True)
        (provider/'plugin.yaml').write_text('name: antigravity-oauth\n')
        m.SOURCE.parent.mkdir(mode=0o700)
        m.SOURCE.write_text(json.dumps({'cuentas':[{'id':'one','nombre':'Ficticio','carnet':'FAKE','password':'SECRET'}]}));m.SOURCE.chmod(0o600)
        self.owner=SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid())
        self.fail_stage=None
        self.fail_dependency=None
        self.read_ok=True
        self.restarts=0
        self.saved_umask=os.umask(0o077)
    def tearDown(self):
        os.umask(self.saved_umask);self.temp.cleanup()
    def fake_run(self, argv, **kwargs):
        m=self.install
        if m.PHASE==self.fail_dependency:
            return SimpleNamespace(returncode=1)
        if argv[:2]==['systemctl','restart']:
            self.restarts+=1
            (m.HOME/'gateway_state.json').write_text(json.dumps({'pid':101,'served_profiles':['default','otro','mark'],
                'platforms':{'telegram':{'state':'connected'}}}))
        if '-m' in argv and 'venv' in argv:
            exe=Path(argv[-1])/'bin/python';exe.parent.mkdir(parents=True);exe.touch()
        if 'playwright' in argv and 'install' in argv and 'chromium' in argv:
            browser=m.ACCOUNT/'.cache/ms-playwright/chromium_headless_shell-test/chrome-headless-shell-linux64/chrome-headless-shell'
            browser.parent.mkdir(parents=True,exist_ok=True);browser.touch()
        if '-c' in argv:
            code=argv[argv.index('-c')+1]
            for stage in ('verify','prepare','routing','activate','pause','daily'):
                if repr(stage)+'];' in code:
                    if stage==self.fail_stage: return SimpleNamespace(returncode=1)
                    report={'stage':stage}
                    if stage=='daily': report['jobs']=[{'task':'review','enabled':True},{'task':'sunday','enabled':True}]
                    (m.PROFILE/f'biblioteca-check-{stage}.json').write_text(json.dumps(report))
        if str(m.PROFILE/'biblioteca-worker.py') in argv:
            view={'accounts':[{'id':'one','name':'Ficticio','checked':datetime.now().astimezone().isoformat(),
                               'loans':[] if self.read_ok else None,'error':None if self.read_ok else 'login_failed'}]}
            return SimpleNamespace(returncode=0,stdout=json.dumps(view))
        return SimpleNamespace(returncode=0,stdout='')
    def fake_output(self, argv, **kwargs):
        if '--print-runtime-command' in argv:return json.dumps(['python','-u','-c','placeholder'])
        return '101\n'
    def invoke(self,*args):
        m=self.install
        with ExitStack() as stack:
            stack.enter_context(patch.object(m.os,'geteuid',return_value=0))
            stack.enter_context(patch.object(m.os,'getgrouplist',return_value=[os.getgid()]))
            stack.enter_context(patch.object(m.pwd,'getpwnam',return_value=self.owner))
            stack.enter_context(patch('grp.getgrgid',return_value=SimpleNamespace(gr_name='hermes')))
            stack.enter_context(patch.object(m.subprocess,'run',side_effect=self.fake_run))
            stack.enter_context(patch.object(m.subprocess,'check_output',side_effect=self.fake_output))
            stack.enter_context(patch.object(m,'metadata',side_effect=lambda token,method,payload=None:
                {'getChat':{'title':'MARK','type':'group'},'getMe':{'id':42,'can_read_all_group_messages':True},
                 'getChatMember':{'status':'administrator'}}[method]))
            stack.enter_context(patch('sys.argv',['install.py',*args]))
            stack.enter_context(redirect_stdout(io.StringIO()))
            return m.main()
    def test_prepare_leaves_profile_parked_and_primary_untouched(self):
        self.invoke('--prepare')
        m=self.install
        self.assertTrue((m.PROFILE/'gateway.parked').exists())
        self.assertEqual((m.HOME/'config.yaml').read_bytes(),self.original)
        self.assertFalse((m.PROFILE/'biblioteca-secrets/credenciales.json').exists())
        self.assertEqual(self.restarts,0)
    def test_activation_after_positive_read_preserves_otro(self):
        self.invoke('--group-id','-2')
        m=self.install
        self.assertFalse((m.PROFILE/'gateway.parked').exists())
        raw=__import__('yaml').safe_load((m.HOME/'config.yaml').read_text())
        self.assertEqual(raw['profile_routes'][0]['profile'],'otro')
        self.assertEqual(raw['profile_routes'][1]['profile'],'mark')
        report=json.loads(next(m.EVIDENCE.glob('*.json')).read_text())
        self.assertTrue(report['activated']);self.assertFalse(report['renewal_live_verified'])
        self.assertNotIn('SECRET',json.dumps(report));self.assertNotIn('FAKE_TOKEN',json.dumps(report))
    def test_failed_account_read_never_activates_or_restarts(self):
        self.read_ok=False
        with self.assertRaisesRegex(RuntimeError,'library_read_failed'):self.invoke('--group-id','-2')
        m=self.install
        self.assertEqual((m.HOME/'config.yaml').read_bytes(),self.original)
        self.assertTrue((m.PROFILE/'gateway.parked').exists());self.assertEqual(self.restarts,0)
    def test_failed_activation_restores_config_and_absent_voice_file(self):
        self.fail_stage='activate'
        with self.assertRaisesRegex(RuntimeError,'private_operation_failed'):self.invoke('--group-id','-2')
        m=self.install
        self.assertEqual((m.HOME/'config.yaml').read_bytes(),self.original)
        self.assertFalse((m.HOME/'gateway_voice_mode.json').exists())
        self.assertTrue((m.PROFILE/'gateway.parked').exists())
        self.assertEqual(self.restarts,2)
        self.assertEqual(m.PHASE,'native_activate')
    def test_active_profile_not_overwritten_by_rerunning_install(self):
        self.invoke('--group-id','-2')
        original=(self.install.PROFILE/'config.yaml').read_bytes()
        with self.assertRaisesRegex(RuntimeError,'profile_already_active'):self.invoke('--group-id','-2')
        self.assertEqual((self.install.PROFILE/'config.yaml').read_bytes(),original)
    def test_failed_account_update_keeps_previous_credentials(self):
        self.invoke('--group-id','-2')
        dest=self.install.PROFILE/'biblioteca-secrets/credenciales.json'
        before=dest.read_bytes();self.read_ok=False
        with self.assertRaisesRegex(RuntimeError,'library_read_failed'):self.invoke('--apply-accounts')
        self.assertEqual(dest.read_bytes(),before)
        self.assertFalse((dest.parent/'candidate.json').exists())
    def test_browser_failure_reports_precise_step_without_activation(self):
        self.fail_dependency='browser_download'
        with self.assertRaisesRegex(RuntimeError,'private_operation_failed'):self.invoke('--group-id','-2')
        m=self.install
        self.assertEqual(m.PHASE,'browser_download')
        self.assertEqual(m.SAFE_DIAGNOSTIC['operation'],'browser_download')
        self.assertEqual(m.SAFE_DIAGNOSTIC['exit_code'],1)
        self.assertTrue((m.PROFILE/'gateway.parked').exists())
        self.assertEqual(self.restarts,0)
    def test_private_log_diagnostic_exposes_only_fixed_categories(self):
        log=self.install.ACCOUNT/'migration/mark-biblioteca-20261022T092602Z/operations.log'
        log.parent.mkdir(parents=True)
        log.write_text('Successfully installed playwright\nChromium downloaded to /private/SECRET\nInstalling dependencies...\nE: Failed to fetch https://FAKE_TOKEN:SECRET@example.invalid/account\n')
        self.invoke('--diagnose-last-failure')
        report=json.loads(next(self.install.EVIDENCE.glob('*diagnostic-*.json')).read_text())
        text=json.dumps(report)
        self.assertNotIn('SECRET',text);self.assertNotIn('FAKE_TOKEN',text)
        self.assertEqual(report['details']['reasons'],['network'])
        self.assertTrue(report['details']['system_install_started'])
        self.assertFalse(report['services_changed']);self.assertEqual(self.restarts,0)
        self.assertFalse(self.install.PROFILE.exists())
    def test_headless_installs_only_packages_for_missing_libraries(self):
        m=self.install
        with patch.object(m.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='libatk-1.0.so.0 => not found\nlibasound.so.2 => not found\n')), \
             patch.object(m.platform,'freedesktop_os_release',return_value={'ID':'ubuntu','VERSION_ID':'24.04'}):
            self.assertEqual(m.missing_browser_packages([Path('/fake/browser')]),['libasound2t64','libatk1.0-0t64'])
        with patch.object(m.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='libunknown.so.1 => not found\n')):
            with self.assertRaisesRegex(RuntimeError,'browser_missing_library_unmapped'):
                m.missing_browser_packages([Path('/fake/browser')])
    def test_daily_update_preserves_credentials_and_primary_without_restart(self):
        self.invoke('--group-id','-2');m=self.install
        for name in ('cron/jobs.json','biblioteca-data/jobs.json'):
            (m.PROFILE/name).write_text('{}\n')
        primary=(m.HOME/'config.yaml').read_bytes()
        credentials=(m.PROFILE/'biblioteca-secrets/credenciales.json').read_bytes()
        restarts=self.restarts
        m.SOURCE.unlink()  # Updating frequency must not require reading account secrets.
        self.invoke('--daily-review')
        report=json.loads(next(m.EVIDENCE.glob('*daily-*.json')).read_text())
        self.assertTrue(report['daily_review_configured'])
        self.assertFalse(report['gateway_restarted']);self.assertFalse(report['renewals_triggered'])
        self.assertEqual(self.restarts,restarts)
        self.assertEqual((m.HOME/'config.yaml').read_bytes(),primary)
        self.assertEqual((m.PROFILE/'biblioteca-secrets/credenciales.json').read_bytes(),credentials)
        self.assertEqual(json.loads((m.PROFILE/'biblioteca-install.json').read_text())['review_cadence'],'daily')
        self.assertTrue((Path(report['backup'])/'cron/jobs.json').exists())


if __name__=='__main__':unittest.main(verbosity=2)
