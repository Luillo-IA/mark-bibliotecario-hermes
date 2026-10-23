import copy
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('mark_test_plugin',PACKAGE/'plugin/__init__.py',submodule_search_locations=[str(PACKAGE/'plugin')])
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)
from mark_test_plugin import core, web

A=core.Account('test','Lector ficticio','CARNET_FICTICIO','SECRETO_FICTICIO')
LOAN={'id':'a','title':'Libro ficticio','due':'2026-10-26','renewable':True}
MON=datetime(2026,10,26,9,tzinfo=core.ZONE)
SUN=datetime(2026,10,25,9,tzinfo=core.ZONE)


class ConversationTests(unittest.TestCase):
    def test_questions_read_persisted_table_without_browser_or_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            core.run(root/'biblioteca-data',[A],lambda _:Session(),now=MON)
            with patch.object(module,'home',return_value=root), \
                 patch.object(module,'run_worker',side_effect=AssertionError('browser forbidden')), \
                 patch.object(module.subprocess,'Popen',side_effect=AssertionError('process forbidden')):
                for action in ('estado','consultar'):
                    result=json.loads(module.handle({'action':action}))
                    self.assertEqual(result['accounts'][0]['loans'][0]['title'],LOAN['title'])
                    self.assertEqual(result['accounts'][0]['checked'],MON.isoformat())
                    self.assertEqual(result['source'],'local_table')
                    self.assertIn('26/10/2026 09:00',result['summary'])
                self.assertFalse((root/'biblioteca-secrets').exists())

    def test_chat_cannot_renew_or_update_online(self):
        with patch.object(module,'home',side_effect=AssertionError('invalid action reached home')), \
             patch.object(module,'run_worker',side_effect=AssertionError('browser forbidden')):
            for action in ('renovar','read','renew','actualizar'):
                self.assertEqual(json.loads(module.handle({'action':action}))['error'],'invalid_action')

    def test_tool_exposes_only_local_actions(self):
        from types import SimpleNamespace
        calls=[]
        module.register(SimpleNamespace(register_tool=lambda **kwargs:calls.append(kwargs)))
        self.assertEqual(calls[0]['schema']['parameters']['properties']['action']['enum'],['estado','consultar'])


class Session:
    def __init__(self, loans=None, outcome='ok'):
        self.loans=copy.deepcopy([LOAN] if loans is None else loans)
        self.calls=0
        self.reads=0
        self.outcome=outcome
        self.closed=False
    def read(self):
        self.reads+=1
        if self.outcome=='read_error' or self.outcome=='after_click_error' and self.calls:
            raise RuntimeError('SECRET_URL_CARNET')
        return copy.deepcopy(self.loans)
    def renew(self, key):
        self.calls+=1
        if self.outcome=='ok' or self.outcome=='timeout_but_applied':
            self.loans[0]['due']='2026-11-10'
            self.loans[0]['renewable']=False
        if self.outcome!='ok': raise TimeoutError('SECRET_URL_CARNET')
    def close(self): self.closed=True


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'data'
    def tearDown(self): self.temp.cleanup()
    def execute(self, session, renew=False):
        return core.run(self.root,[A],lambda _:session,renew=renew,now=MON)
    def test_read_never_renews(self):
        session=Session(); result=self.execute(session)
        self.assertEqual(session.calls,0)
        self.assertEqual(result['accounts'][0]['loans'][0]['due'],'2026-10-26')
        self.assertTrue(session.closed)
    def test_zero_is_persisted(self):
        result=self.execute(Session([]))
        self.assertEqual(result['accounts'][0]['loans'],[])
        self.assertIn('Sin préstamos activos',core.summary(result,MON))
    def test_failed_read_keeps_snapshot(self):
        self.execute(Session())
        result=self.execute(Session(outcome='read_error'))
        self.assertEqual(result['accounts'][0]['loans'][0]['due'],'2026-10-26')
        self.assertEqual(result['accounts'][0]['error'],'portal_unavailable')
        self.assertNotIn('SECRET',json.dumps(result))
    def test_first_failed_read_is_unknown_not_empty(self):
        result=self.execute(Session(outcome='read_error'))
        self.assertIsNone(result['accounts'][0]['loans'])
        self.assertNotIn('Sin préstamos activos',core.summary(result,MON))
    def test_renewal_requires_fresh_later_date(self):
        session=Session(); result=self.execute(session,True)
        self.assertEqual(session.calls,1)
        self.assertEqual(result['accounts'][0]['loans'][0]['due'],'2026-11-10')
        store=core.Store(self.root)
        self.assertEqual(store.db.execute('SELECT status FROM renewals').fetchone()[0],'confirmed')
        store.close()
    def test_timeout_applied_is_reconciled(self):
        session=Session(outcome='timeout_but_applied'); result=self.execute(session,True)
        self.assertEqual(session.calls,1)
        self.assertEqual(result['uncertain_renewals'],0)
    def test_uncertain_click_is_not_repeated_next_run(self):
        first=Session(outcome='timeout')
        self.execute(first,True)
        second=Session(); result=self.execute(second,True)
        self.assertEqual(second.calls,0)
        self.assertEqual(result['uncertain_renewals'],1)
        self.assertEqual(result['accounts'][0]['error'],'renewal_unconfirmed')
    def test_pending_after_crash_reconciles_without_click(self):
        self.execute(Session())
        store=core.Store(self.root);store.renewal(A,LOAN,MON,'pending');store.close()
        session=Session([{**LOAN,'due':'2026-11-10','renewable':True}])
        result=self.execute(session,True)
        self.assertEqual(session.calls,0)
        self.assertEqual(result['uncertain_renewals'],0)
    def test_error_after_click_preserves_before_and_journal(self):
        result=self.execute(Session(outcome='after_click_error'),True)
        self.assertEqual(result['accounts'][0]['loans'][0]['due'],'2026-10-26')
        self.assertEqual(result['uncertain_renewals'],1)
    def test_noneligible_never_clicked(self):
        session=Session([{**LOAN,'renewable':False,'reason':'suspended'}]);self.execute(session,True)
        self.assertEqual(session.calls,0)
    def test_reservation_failure_preserves_confirmed_renewal(self):
        class WithReservations(Session):
            def reservations(self): raise RuntimeError('portal error')
        result=self.execute(WithReservations(),True)
        self.assertEqual(result['accounts'][0]['loans'][0]['due'],'2026-11-10')
        self.assertEqual(result['accounts'][0]['reservations']['status'],'unverified')
    def test_duplicate_identity_fails_closed(self):
        result=self.execute(Session([LOAN,LOAN]),True)
        self.assertEqual(result['accounts'][0]['error'],'ambiguous_loan')
    def test_accounts_independent_and_removed_inactive(self):
        b=core.Account('other','Otro lector','OTRO','OTRA')
        result=core.run(self.root,[A,b],lambda a:Session(outcome='read_error' if a.id==A.id else 'ok'),now=MON)
        self.assertEqual(len(result['accounts']),2)
        result=self.execute(Session())
        self.assertEqual([a['id'] for a in result['accounts']],['test'])
    def test_lock_prevents_concurrent_renewals(self):
        with core.exclusive(self.root):
            with self.assertRaisesRegex(core.LibraryError,'busy'): self.execute(Session(),True)
    def test_db_restoration(self):
        self.execute(Session())
        store=core.Store(self.root)
        backup=sqlite3.connect(Path(self.temp.name)/'restored.db')
        store.db.backup(backup)
        self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(backup.execute('SELECT loans FROM accounts').fetchone()[0],store.db.execute('SELECT loans FROM accounts').fetchone()[0])
        backup.close();store.close()
    def test_report_sorting_and_dates(self):
        result=self.execute(Session([{**LOAN,'id':'b','title':'Más tarde','due':'2026-11-03'},LOAN]))
        text=core.summary(result,MON)
        self.assertLess(text.index('Libro ficticio'),text.index('Más tarde'))
        self.assertIn('26/10/2026',text)
    def test_monday_delivery_has_no_side_effects(self):
        called=[]
        self.assertEqual(core.sunday_delivery(self.root,'texto',called.append,now_fn=lambda:MON)['status'],'outside_sunday')
        self.assertFalse(called)
    def test_sunday_only_once_even_if_message_changes(self):
        called=[]
        core.sunday_delivery(self.root,'texto',called.append,now_fn=lambda:SUN)
        core.sunday_delivery(self.root,'otro texto más largo',called.append,now_fn=lambda:SUN)
        self.assertEqual(called,['texto'])
    def test_delivery_timeout_never_retried(self):
        calls=[]
        def fail(body): calls.append(body);raise RuntimeError('token')
        result=core.sunday_delivery(self.root,'texto',fail,now_fn=lambda:SUN)
        self.assertEqual(result['status'],'unknown')
        core.sunday_delivery(self.root,'texto',fail,now_fn=lambda:SUN)
        self.assertEqual(len(calls),1)
    def test_delivery_stops_at_day_boundary(self):
        called=[]; ticks=iter([SUN,SUN,MON])
        result=core.sunday_delivery(self.root,'x'*5000,called.append,now_fn=lambda:next(ticks))
        self.assertEqual(len(called),1)
        self.assertEqual(result['status'],'window_closed')
    def test_chunks_handle_long_lines_and_html_as_plaintext(self):
        text='a'*8000+'\n<&Título>'
        parts=core.chunks(text)
        self.assertTrue(all(len(p)<=3500 for p in parts))
        self.assertIn('<&Título>',parts[-1])
    def test_credentials_validate_without_disclosing_values(self):
        private=Path(self.temp.name)/'secrets';private.mkdir(mode=0o700)
        path=private/'accounts.json'
        path.write_text(json.dumps({'cuentas':[{'id':'one','nombre':'Uno','carnet':'123','password':'SECRET'}]}));path.chmod(0o600)
        account=core.load_accounts(path)[0]
        self.assertNotIn('SECRET',repr(account))
        self.assertNotIn('123',repr(account))
        path.chmod(0o644)
        with self.assertRaisesRegex(core.LibraryError,'credentials_permissions'):core.load_accounts(path)
    def test_invalid_json_does_not_leak_secret(self):
        private=Path(self.temp.name)/'secrets';private.mkdir(mode=0o700)
        path=private/'accounts.json';path.write_text('SECRET malformed');path.chmod(0o600)
        with self.assertRaisesRegex(core.LibraryError,'credentials_unreadable'): core.load_accounts(path)


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.manager=sync_playwright().start()
        cls.browser=cls.manager.chromium.launch(headless=True)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.manager.stop()
    def setUp(self): self.page=self.browser.new_page()
    def tearDown(self): self.page.close()
    def card(self,title='Libro ficticio',id='one',due='26/10/2026',extra='',button=True):
        return f'<article data-loan-id="{id}"><h3>{title}</h3><div>Fecha de devolución: {due}</div><p>Renovaciones 0 / 1</p>{extra}'+('<button onclick="this.closest(\'article\').outerHTML=this.closest(\'article\').outerHTML.replace(\'26/10/2026\',\'10/11/2026\')">Renovar 15 días</button>' if button else '')+'</article>'
    def session(self,body,expected=1):
        self.page.set_content(body)
        return web.PortalSession.fixture(self.page,expected)
    def test_browser_read_and_renew_after_dom_replacement(self):
        session=self.session(self.card())
        loan=session.read()[0]
        self.assertEqual(loan['due'],'2026-10-26')
        session.renew(loan['id'])
        self.assertEqual(session.read()[0]['due'],'2026-11-10')
    def test_browser_missing_cards_is_not_zero(self):
        session=self.session('<h1>Portal no disponible</h1>')
        with self.assertRaisesRegex(core.LibraryError,'incomplete_loans'):session.read()
    def test_browser_real_zero(self):
        session=self.session('<h1>0 préstamos</h1>',0)
        self.assertEqual(session.read(),[])
    def test_browser_hidden_responsive_cards_not_duplicated(self):
        session=self.session(self.card()+'<div style="display:none">'+self.card()+'</div>')
        self.assertEqual(len(session.read()),1)
    def test_browser_two_same_titles_have_distinct_ids(self):
        session=self.session(self.card()+self.card(id='two'),2)
        self.assertEqual(len({l['id'] for l in session.read()}),2)
    def test_browser_ambiguous_same_title_without_ids_rejected(self):
        session=self.session((self.card()+self.card()).replace('data-loan-id="one"',''),2)
        with self.assertRaisesRegex(core.LibraryError,'ambiguous_loan'):session.read()
    def test_browser_suspended_blocks_visible_renew_button(self):
        session=self.session(self.card(extra='<p>Lector suspendido. No se puede renovar</p>'))
        self.assertFalse(session.read()[0]['renewable'])
    def test_browser_not_yet_and_limit(self):
        session=self.session(self.card(extra='<p>Renovable a partir de 25/10/2026</p>'))
        self.assertFalse(session.read()[0]['renewable'])
        session=self.session(self.card().replace('0 / 1','1 / 1'))
        self.assertFalse(session.read()[0]['renewable'])
    def test_browser_no_positive_session_cannot_open_loans(self):
        session=self.session('<h1>Área de usuario</h1>')
        with self.assertRaisesRegex(core.LibraryError,'loan_count_missing'):session.open_loans()
    def login_session(self, body):
        # All official-host requests are intercepted; fake login never reaches a server.
        self.page.route('https://www.comunidad.madrid/**',lambda route:route.fulfill(body=body,content_type='text/html; charset=utf-8'))
        self.page.goto(web.URL)
        session=web.PortalSession.fixture(self.page,1)
        session.context=self.page.context
        session.fixture_mode=False
        return session
    def test_browser_positive_login_and_count(self):
        session=self.login_session('''<form onsubmit="event.preventDefault();document.body.innerHTML='<a>Cerrar sesión</a><button>0 préstamos</button>'">
            <input type=text><input type=password><button type=submit>Entrar</button></form>''')
        session.login(A);session.open_loans()
        self.assertEqual(session.read(),[])
    def test_browser_login_without_positive_signal_is_rejected(self):
        session=self.login_session('''<form onsubmit="event.preventDefault();document.body.innerHTML='<h1>Bienvenido</h1>'">
            <input type=text><input type=password><button type=submit>Entrar</button></form>''')
        with self.assertRaisesRegex(core.LibraryError,'login_unconfirmed'):session.login(A)
    def test_browser_rejects_credential_form_to_other_host(self):
        session=self.login_session('''<form action="https://example.invalid/steal">
            <input type=text><input type=password><button type=submit>Entrar</button></form>''')
        with self.assertRaisesRegex(core.LibraryError,'unexpected_login_host'):session.login(A)
        self.assertEqual(self.page.locator('input[type=password]').input_value(),'')
    def test_browser_zero_reservations_is_confirmed(self):
        session=self.session('<button>0 reservas</button>')
        self.assertEqual(session.reservations(),{'status':'ok','items':[]})
    def test_actual_portal_plural_count_labels(self):
        for text,count in [('4 préstamo(s)',4),('Préstamos 4',4),('0 préstamo(s)',0),('Préstamos (15)',15)]:
            self.assertEqual(web.count_value(web.COUNT.fullmatch(text)),count)
        self.assertEqual(web.count_value(web.count_pattern('reserva').fullmatch('0 reserva(s)')),0)
    def test_actual_portal_nested_card_includes_title_above_details(self):
        session=self.session('''<ol><li><h3><a>Libro ficticio</a></h3><div>
          <p><span>Fecha de devolución: 26/10/2026</span></p><p>Renovaciones 0 / 1</p>
          <a>Biblioteca ficticia</a><button>Renovar 15 días</button></div></li></ol>''')
        self.assertEqual(session.read()[0]['title'],'Libro ficticio')
    def test_portal_counter_can_render_after_session_header(self):
        session=self.login_session('''<a>Cerrar sesión</a><div id="count"></div>
          <script>setTimeout(()=>document.querySelector('#count').innerHTML='<button>0 préstamo(s)</button>',900)</script>''')
        session.open_loans()
        self.assertEqual(session.expected,0)
    def test_actual_empty_current_template_does_not_count_history(self):
        session=self.session('''<section><h2 id="section_lectorPrest">Préstamos</h2>
          <div class="js-lector_biblioPresta"><div class="c-accordion_item" id="lector_PH">
          <p>Histórico Préstamos15</p><article id="lector_PH_detail" style="display:none">
          <ol><li>Libro histórico ficticio</li></ol></article></div></div></section>''')
        session.open_loans()
        self.assertEqual(session.expected,0)
        self.assertEqual(session.read(),[])
    def test_browser_existing_reservation_read_without_creation(self):
        session=self.session('<button id="reservas">1 reserva</button>')
        self.page.evaluate('''() => document.querySelector('#reservas').onclick=()=>document.body.innerHTML=
          '<article data-reservation-id="one"><h3>Reserva ficticia</h3><p>Recoger hasta: 28/10/2026</p></article>' ''')
        result=session.reservations()
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['items'],[{'title':'Reserva ficticia','pickup_due':'2026-10-28'}])


if __name__=='__main__': unittest.main(verbosity=2)
