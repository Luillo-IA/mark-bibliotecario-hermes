from datetime import datetime
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from test_library import Session, MON, SUN

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('mark_task_test',HERE/'task.py')
task=importlib.util.module_from_spec(spec);spec.loader.exec_module(task)


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.profile=Path(self.temp.name)/'profiles/mark'
        shutil.copytree(HERE/'plugin',self.profile/'plugins/mark-biblioteca')
        private=self.profile/'biblioteca-secrets';private.mkdir(mode=0o700)
        p=private/'credenciales.json'
        p.write_text(json.dumps({'cuentas':[{'id':'test','nombre':'Ficticio','carnet':'TEST','password':'SECRET'}]}));p.chmod(0o600)
    def tearDown(self): self.temp.cleanup()
    def test_monday_reviews_renew_without_delivery(self):
        calls=[];session=Session()
        task.execute(self.profile,'review',now=MON,backend=lambda _:session,sender=calls.append)
        self.assertEqual(session.calls,1)
        self.assertFalse(calls)
    def test_sunday_renews_before_reporting_updated_dates(self):
        calls=[];session=Session()
        task.execute(self.profile,'sunday',now=SUN,clock_fn=lambda:SUN,backend=lambda _:session,sender=calls.append)
        self.assertEqual(session.calls,1)
        self.assertEqual(len(calls),1)
        self.assertIn('Libro ficticio',calls[0]);self.assertIn('10/11/2026',calls[0])
        self.assertNotIn('SECRET',calls[0]);self.assertNotIn('TEST',calls[0])
    def test_delayed_sunday_job_does_not_send_on_monday(self):
        calls=[];session=Session()
        result=task.execute(self.profile,'sunday',now=MON,backend=lambda _:session,sender=calls.append)
        self.assertEqual(result['status'],'outside_sunday')
        self.assertEqual(session.reads,0);self.assertFalse(calls)
    def test_reviews_renew_each_day_monday_to_saturday_without_messages(self):
        for day in range(26,32):
            with self.subTest(day=day):
                session=Session();calls=[]
                # Separate loan identities represent a fresh eligible loan on each day.
                session.loans[0]['id']=str(day)
                task.execute(self.profile,'review',now=MON.replace(day=day),backend=lambda _:session,sender=calls.append)
                self.assertEqual(session.calls,1)
                self.assertFalse(calls)
    def test_sunday_silent_job_leaves_review_to_sunday_report_job(self):
        session=Session();calls=[]
        result=task.execute(self.profile,'review',now=SUN,backend=lambda _:session,sender=calls.append)
        self.assertEqual(result['status'],'outside_review_days')
        self.assertEqual(session.reads,0);self.assertFalse(calls)
    def test_error_in_silent_review_never_sends(self):
        calls=[]
        result=task.execute(self.profile,'review',now=MON,backend=lambda _:Session(outcome='read_error'),sender=calls.append)
        self.assertEqual(result['status'],'partial');self.assertFalse(calls)
    def test_sunday_error_reports_old_date_as_old_not_empty(self):
        task.execute(self.profile,'review',now=MON,backend=lambda _:Session(),sender=lambda _:None)
        calls=[]
        task.execute(self.profile,'sunday',now=SUN,clock_fn=lambda:SUN,backend=lambda _:Session(outcome='read_error'),sender=calls.append)
        self.assertIn('incidencia',calls[0]);self.assertIn('Última consulta',calls[0]);self.assertIn('10/11/2026',calls[0])
    def test_sunday_credentials_failure_still_reports_incidence(self):
        (self.profile/'biblioteca-secrets/credenciales.json').unlink()
        calls=[]
        task.execute(self.profile,'sunday',now=SUN,clock_fn=lambda:SUN,sender=calls.append)
        self.assertEqual(len(calls),1);self.assertIn('No se ha podido actualizar',calls[0])


if __name__=='__main__':unittest.main(verbosity=2)
