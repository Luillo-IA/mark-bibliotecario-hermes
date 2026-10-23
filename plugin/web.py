"""Adaptador Playwright del Portal del Lector. No guarda HTML, cookies o capturas."""
from datetime import datetime
import hashlib
import re
import time
from urllib.parse import urlparse
from .core import LibraryError, normalize_loans, parse_date

URL='https://www.comunidad.madrid/portal-lector/area-usuario'
ALLOWED={'www.comunidad.madrid','comunidad.madrid','gestiona.comunidad.madrid'}
DATE=re.compile(r'fecha de devoluci[oó]n\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4})',re.I)
def count_pattern(word):
    label=word+r'(?:s|\(s\))?'
    return re.compile(r'^\s*(?:(\d+)\s*'+label+r'|'+label+r'\s*[:(]?\s*(\d+)\s*\)?)\s*$',re.I)


COUNT=count_pattern(r'pr[eé]stamo')


def count_value(match):
    return int(next(value for value in match.groups() if value is not None))


def to_iso(value):
    try:
        day,month,year=value.split('/')
        if len(year)==2: year='20'+year
        return datetime(int(year),int(month),int(day)).date().isoformat()
    except Exception:
        raise LibraryError('invalid_date') from None


def ensure_host(page):
    parsed=urlparse(page.url)
    if parsed.scheme!='https' or parsed.hostname not in ALLOWED:
        raise LibraryError('unexpected_portal')


class PortalSession:
    def __init__(self, account, *, playwright=None, executable=None):
        self.manager=None
        self.browser=None
        self.context=None
        try:
            if playwright is None:
                from playwright.sync_api import sync_playwright
                self.manager=sync_playwright().start()
                playwright=self.manager
            kwargs={'headless':True}
            if executable: kwargs['executable_path']=executable
            self.browser=playwright.chromium.launch(**kwargs)
            self.context=self.browser.new_context(locale='es-ES',timezone_id='Europe/Madrid')
            self.context.set_default_timeout(8000)
            self.page=self.context.new_page()
            self.page.goto(URL,wait_until='domcontentloaded',timeout=30000)
            self.login(account)
            self.open_loans()
        except Exception as exc:
            self.close()
            if isinstance(exc,LibraryError): raise
            raise LibraryError('portal_unavailable') from None

    @classmethod
    def fixture(cls, page, expected):
        """Existing local page for browser tests. No login and no external requests."""
        self=cls.__new__(cls)
        self.page=page
        self.expected=expected
        self.manager=self.browser=self.context=None
        self.fixture_mode=True
        return self

    def contexts(self):
        frames=[self.page]+[f for f in self.page.frames if f!=self.page.main_frame]
        if getattr(self,'fixture_mode',False): return frames
        return [f for f in frames if urlparse(f.url).scheme=='https' and urlparse(f.url).hostname in ALLOWED]

    def settle(self):
        self.page.wait_for_timeout(800)
        if not getattr(self,'fixture_mode',False): ensure_host(self.page)

    def login(self, account):
        for step in range(2):
            self.settle()
            found=None
            for frame in self.contexts():
                fields=frame.locator("input[type=password]")
                for i in range(fields.count()):
                    if fields.nth(i).is_visible():
                        found=(frame,fields.nth(i))
                        break
                if found: break
            if not found: break
            frame,password=found
            # Never fill credentials into a redirected third-party frame.
            parsed=urlparse(frame.url)
            if parsed.scheme!='https' or parsed.hostname not in ALLOWED:
                raise LibraryError('unexpected_login_host')
            form=password.locator('xpath=ancestor::form[1]')
            if not form.count(): raise LibraryError('login_form_changed')
            action=form.get_attribute('action')
            if action:
                from urllib.parse import urljoin
                target=urlparse(urljoin(frame.url,action))
                if target.scheme!='https' or target.hostname not in ALLOWED:
                    raise LibraryError('unexpected_login_host')
            form.locator("input[type=text],input[type=tel],input:not([type])").first.fill(account.card)
            password.fill(account.password)
            button=form.locator("input[type=submit],button[type=submit],input[type=image],button:has-text('Entrar')").first
            # One click only, including when no popup arrives.
            pages_before=list(self.context.pages)
            button.click()
            self.page.wait_for_timeout(1600)
            new=[p for p in self.context.pages if p not in pages_before]
            if new:
                self.page=new[-1]
                self.page.wait_for_load_state('domcontentloaded',timeout=20000)
        self.settle()
        body='\n'.join(frame.locator('body').inner_text() for frame in self.contexts())
        if re.search(r'contrase[nñ]a incorrecta|usuario incorrecto|no v[aá]lido',body,re.I):
            raise LibraryError('login_failed')
        if any(f.locator('input[type=password]:visible').count() for f in self.contexts()):
            raise LibraryError('login_failed')
        if not re.search(r'cerrar sesi[oó]n|desconectar',body,re.I):
            raise LibraryError('login_unconfirmed')

    def open_loans(self):
        self.on_loans_page=False
        self.expected=None
        for attempt in range(24 if not getattr(self,'fixture_mode',False) else 1):
            for frame in self.contexts():
                nodes=frame.get_by_text(COUNT)
                for i in range(nodes.count()):
                    el=nodes.nth(i)
                    if el.is_visible():
                        self.expected=count_value(COUNT.fullmatch(el.inner_text().strip()))
                        if self.expected==0: return
                        el.click()
                        self.settle()
                        self.on_loans_page=True
                        return
            self.page.wait_for_timeout(250)
        # Current AbsysNet renders no "0 préstamo(s)" tile: the completed loans
        # accordion contains ONLY its separate history panel. Match this exact
        # authenticated template; absence of arbitrary cards is never enough.
        for frame in self.contexts():
            heading=frame.locator('#section_lectorPrest')
            if heading.count()!=1 or not heading.is_visible() or heading.inner_text().strip()!='Préstamos':
                continue
            section=heading.locator('xpath=ancestor::section[1]')
            region=section.locator('.js-lector_biblioPresta')
            if region.count()!=1: continue
            panels=region.locator(':scope > .c-accordion_item')
            if (panels.count()==1 and panels.first.get_attribute('id')=='lector_PH'
                    and panels.first.locator('#lector_PH_detail').count()==1
                    and panels.first.get_by_text(re.compile(r'Hist[oó]rico\s*Pr[eé]stamos?\s*\d+',re.I)).count()>0):
                self.expected=0
                return
        # An unverified generic navigation is not an authoritative zero.
        raise LibraryError('loan_count_missing')

    def read(self):
        if self.expected==0: return []
        if not getattr(self,'fixture_mode',False): ensure_host(self.page)
        # Trigger lazy cards, stopping after stable height. Never retain session storage.
        height=-1
        for _ in range(6):
            self.page.mouse.wheel(0,3000)
            self.page.wait_for_timeout(150)
            current=self.page.evaluate('document.body.scrollHeight')
            if current==height: break
            height=current
        self.cards={}
        loans=[]
        used_nodes=set()
        for frame_index,frame in enumerate(self.contexts()):
            labels=frame.locator('text=/[Ff]echa de devoluci[oó]n/')
            for i in range(labels.count()):
                label=labels.nth(i)
                if not label.is_visible(): continue
                card=label
                text=''
                title=None
                for _ in range(8):
                    card=card.locator('xpath=..')
                    text=card.inner_text()
                    if len(DATE.findall(text))>1:
                        raise LibraryError('loan_card_changed')
                    if re.search(r'renovaci[oó]n|renovaciones|no se puede renovar|renovable',text,re.I):
                        titles=card.locator('h1,h2,h3,h4,a').all_inner_texts()
                        title=next((t.strip().rstrip('/').strip() for t in titles
                                    if t.strip() and not re.match(r'Biblioteca|Renovar|Reservar',t.strip(),re.I)),None)
                        if title:break
                matches=DATE.findall(text)
                if len(matches)!=1:
                    raise LibraryError('loan_card_changed')
                # Bound title to this card; do not accidentally read a preceding branch.
                if not title: raise LibraryError('loan_title_missing')
                raw_id=card.get_attribute('data-loan-id') or card.get_attribute('data-prestamo-id')
                if raw_id:
                    key='id-'+hashlib.sha256(raw_id.encode()).hexdigest()[:24]
                else:
                    # Unique titles only. A repeated identical title is ambiguous without a portal ID.
                    key='title-'+hashlib.sha256(title.encode()).hexdigest()[:24]
                identity=(frame_index,card.evaluate('(e)=>{ if(!e.dataset.markNode) e.dataset.markNode=String(++window.markNodeSeq || (window.markNodeSeq=1));return e.dataset.markNode;}'))
                if identity in used_nodes: continue
                used_nodes.add(identity)
                if key in self.cards: raise LibraryError('ambiguous_loan')
                buttons=card.locator("a:has-text('Renovar'),button:has-text('Renovar'),[title*='Renovar' i],[aria-label*='Renovar' i],input[value*='Renovar' i],img[alt*='Renovar' i]")
                available=False
                for j in range(buttons.count()):
                    if buttons.nth(j).is_visible() and buttons.nth(j).is_enabled(): available=True
                reason=None
                for pattern,code in [(r'lector suspendido','suspended'),(r'pr[eé]stamos sobrepasados','overdue'),
                                     (r'l[ií]mite de renovaciones','limit'),(r'renovable a partir','not_yet'),
                                     (r'reservado|reservas de otro','reserved'),(r'no se puede renovar|no renovable','unavailable')]:
                    if re.search(pattern,text,re.I): reason=code;break
                counter=re.search(r'renovaciones\s*(\d+)\s*/\s*(\d+)',text,re.I)
                if counter and int(counter[1])>=int(counter[2]): reason='limit'
                loan={'id':key,'title':title,'due':to_iso(matches[0]),'renewable':available and reason is None,'reason':reason}
                loans.append(loan)
                self.cards[key]=card
        if len(loans)!=self.expected:
            raise LibraryError('incomplete_loans')
        return normalize_loans(loans)

    def renew(self, loan_id):
        current=self.read()
        loan=next((l for l in current if l['id']==loan_id),None)
        if not loan or not loan['renewable']: raise LibraryError('not_renewable')
        card=self.cards[loan_id]
        candidates=card.locator("a:has-text('Renovar'),button:has-text('Renovar'),input[value*='Renovar' i],[title*='Renovar' i],[aria-label*='Renovar' i],img[alt*='Renovar' i]")
        for i in range(candidates.count()):
            button=candidates.nth(i)
            if button.is_visible() and button.is_enabled():
                button.click()
                # Re-find by stable identity, not stale position, after a DOM replacement.
                for _ in range(6):
                    self.page.wait_for_timeout(700)
                    fresh=self.read()
                    updated=next((l for l in fresh if l['id']==loan_id),None)
                    if updated and parse_date(updated['due'])>parse_date(loan['due']): return
                raise LibraryError('renewal_unconfirmed')
        raise LibraryError('not_renewable')

    def reservations(self):
        if getattr(self,'on_loans_page',False):
            self.page.go_back(wait_until='domcontentloaded')
            self.settle()
        expected=None
        pattern=count_pattern('reserva')
        for frame in self.contexts():
            nodes=frame.get_by_text(pattern)
            for i in range(nodes.count()):
                el=nodes.nth(i)
                if el.is_visible():
                    expected=count_value(pattern.fullmatch(el.inner_text().strip()))
                    if expected==0: return {'status':'ok','items':[]}
                    el.click();self.settle();break
            if expected is not None:break
        if expected is None: return {'status':'unverified','items':[]}
        items=[]
        for frame in self.contexts():
            titles=frame.locator('article h2,article h3,article h4,[data-reservation-id] h2,[data-reservation-id] h3,.reserva h3')
            for i in range(titles.count()):
                title=titles.nth(i)
                if not title.is_visible():continue
                container=title.locator('xpath=ancestor::*[self::article or @data-reservation-id or contains(concat(" ",normalize-space(@class)," ")," reserva ")][1]')
                text=container.inner_text()
                deadline=re.search(r'(?:recoger hasta|fecha l[ií]mite de recogida|fecha de recogida)\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4})',text,re.I)
                items.append({'title':title.inner_text().strip().rstrip('/').strip()[:600],
                              'pickup_due':to_iso(deadline[1]) if deadline else None})
        if len(items)!=expected: return {'status':'unverified','items':[]}
        return {'status':'ok','items':items}

    def close(self):
        for item in (self.context,self.browser,self.manager):
            if item:
                try:
                    item.stop() if item is self.manager else item.close()
                except Exception: pass
