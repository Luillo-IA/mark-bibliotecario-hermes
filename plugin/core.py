"""Estado privado y ejecución determinista de biblioteca. Sin modelos ni Telegram."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, date
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Europe/Madrid')


class LibraryError(Exception):
    """Código cerrado; nunca contiene DOM, URL, contraseña o excepción externa."""


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    card: str = field(repr=False)
    password: str = field(repr=False)


def clock():
    return datetime.now(ZONE)


def load_accounts(path):
    path = Path(path)
    try:
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            raise LibraryError('credentials_file')
        if path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077:
            raise LibraryError('credentials_permissions')
        raw = json.loads(path.read_text())
        if set(raw) != {'cuentas'} or not isinstance(raw['cuentas'], list) or not 1 <= len(raw['cuentas']) <= 20:
            raise LibraryError('credentials_format')
        accounts = []
        for item in raw['cuentas']:
            if not isinstance(item, dict) or set(item) != {'id', 'nombre', 'carnet', 'password'}:
                raise LibraryError('credentials_format')
            if any(not isinstance(v, str) or not v or len(v) > 200 for v in item.values()):
                raise LibraryError('credentials_incomplete')
            if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', item['id']):
                raise LibraryError('account_id')
            if any(ord(c) < 32 for c in item['nombre']):
                raise LibraryError('account_name')
            accounts.append(Account(item['id'], item['nombre'], item['carnet'], item['password']))
        if len({a.id for a in accounts}) != len(accounts) or len({a.card for a in accounts}) != len(accounts):
            raise LibraryError('duplicate_accounts')
        return accounts
    except LibraryError:
        raise
    except Exception:
        raise LibraryError('credentials_unreadable') from None


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise LibraryError('invalid_date') from None


def normalize_loans(loans):
    if not isinstance(loans, list):
        raise LibraryError('loans_format')
    keys = set()
    result = []
    for loan in loans:
        key = loan.get('id')
        title = loan.get('title')
        if not isinstance(key, str) or not key or key in keys or not isinstance(title, str) or not title.strip():
            raise LibraryError('ambiguous_loan')
        keys.add(key)
        parse_date(loan.get('due'))
        # No DOM/locators/URLs enter state or model output.
        result.append({'id': key, 'title': ' '.join(title.split())[:600], 'due': parse_date(loan['due']).isoformat(),
                       'renewable': loan.get('renewable') is True,
                       'reason': loan.get('reason') if loan.get('reason') in
                       ('suspended','overdue','limit','not_yet','reserved','unavailable') else None})
    return result


@contextmanager
def exclusive(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root/'operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LibraryError('busy') from None
        yield
    finally:
        os.close(fd)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root/'biblioteca.sqlite3'
        if path.is_symlink():
            raise LibraryError('state_path')
        self.db = sqlite3.connect(path, timeout=5)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, checked TEXT,
            loans TEXT, error TEXT, active INTEGER NOT NULL DEFAULT 1, reservations TEXT);
          CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, started TEXT NOT NULL, finished TEXT,
            mode TEXT NOT NULL, status TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS renewals (
            account TEXT NOT NULL, loan TEXT NOT NULL, old_due TEXT NOT NULL,
            new_due TEXT, status TEXT NOT NULL, checked TEXT NOT NULL,
            PRIMARY KEY(account,loan,old_due));
          CREATE TABLE IF NOT EXISTS deliveries (
            week TEXT NOT NULL, part INTEGER NOT NULL, digest TEXT NOT NULL,
            status TEXT NOT NULL, checked TEXT NOT NULL,
            PRIMARY KEY(week,part));
        ''')
        if 'reservations' not in {r[1] for r in self.db.execute('PRAGMA table_info(accounts)')}:
            self.db.execute('ALTER TABLE accounts ADD COLUMN reservations TEXT')
            self.db.commit()

    def close(self):
        self.db.close()

    def sync(self, accounts):
        with self.db:
            self.db.execute('UPDATE accounts SET active=0')
            for a in accounts:
                self.db.execute('INSERT INTO accounts(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,active=1', (a.id,a.name))

    def save(self, account, loans, now):
        loans = normalize_loans(loans)
        with self.db:
            self.db.execute('UPDATE accounts SET checked=?,loans=?,error=NULL WHERE id=?',
                            (now.isoformat(), json.dumps(loans,ensure_ascii=False), account.id))

    def error(self, account, code):
        with self.db:
            self.db.execute('UPDATE accounts SET error=? WHERE id=?', (code,account.id))

    def renewal(self, account, loan, now, status, new_due=None):
        with self.db:
            self.db.execute('INSERT INTO renewals VALUES(?,?,?,?,?,?) ON CONFLICT(account,loan,old_due) DO UPDATE SET new_due=excluded.new_due,status=excluded.status,checked=excluded.checked',
                            (account.id,loan['id'],loan['due'],new_due,status,now.isoformat()))

    def view(self):
        rows=[]
        for row in self.db.execute('SELECT * FROM accounts WHERE active=1 ORDER BY id'):
            rows.append({'id':row['id'],'name':row['name'],'checked':row['checked'],
                         'loans':json.loads(row['loans']) if row['loans'] is not None else None,
                         'error':row['error'],
                         'reservations':json.loads(row['reservations']) if row['reservations'] else None})
        last=self.db.execute('SELECT id,started,finished,mode,status FROM runs ORDER BY id DESC LIMIT 1').fetchone()
        uncertain=self.db.execute("SELECT count(*) FROM renewals WHERE status IN ('pending','uncertain')").fetchone()[0]
        return {'accounts':rows,'last_run':dict(last) if last else None,'uncertain_renewals':uncertain}

    def claim_delivery(self, week, part, text, now):
        try:
            with self.db:
                self.db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?)',
                                (week,part,hashlib.sha256(text.encode()).hexdigest(),'pending',now.isoformat()))
            return True
        except sqlite3.IntegrityError:
            return False

    def delivery_result(self, week, part, status):
        with self.db:
            self.db.execute('UPDATE deliveries SET status=? WHERE week=? AND part=?',(status,week,part))


def run(root, accounts, backend, *, renew=False, now=None):
    """Every successful read is committed; later failures cannot erase confirmed renewals."""
    now = now or clock()
    with exclusive(root):
        store=Store(root)
        try:
            store.sync(accounts)
            with store.db:
                cur=store.db.execute('INSERT INTO runs(started,mode,status) VALUES(?,?,?)',
                                     (now.isoformat(),'renew' if renew else 'read','running'))
                run_id=cur.lastrowid
            failed=False
            for account in accounts:
                session=None
                try:
                    session=backend(account)
                    current=normalize_loans(session.read())
                    store.save(account,current,now)
                    if renew:
                        # Work from the initial eligible set only: never renew twice in one run.
                        unresolved={r[0] for r in store.db.execute(
                            "SELECT loan FROM renewals WHERE account=? AND status IN ('pending','uncertain')",(account.id,))}
                        targets=[l for l in current if l['renewable'] and l['id'] not in unresolved]
                        for initial in targets:
                            current=normalize_loans(session.read())
                            store.save(account,current,now)
                            loan=next((l for l in current if l['id']==initial['id']),None)
                            if not loan or loan['due']!=initial['due'] or not loan['renewable']:
                                continue
                            previous=store.db.execute('SELECT * FROM renewals WHERE account=? AND loan=? AND old_due=?',
                                                      (account.id,loan['id'],loan['due'])).fetchone()
                            if previous and previous['status'] in ('pending','uncertain','confirmed'):
                                continue
                            store.renewal(account,loan,now,'pending')
                            try:
                                session.renew(loan['id'])
                            except Exception:
                                pass
                            try:
                                fresh=normalize_loans(session.read())
                                store.save(account,fresh,now)
                                updated=next((l for l in fresh if l['id']==loan['id']),None)
                                if updated and parse_date(updated['due'])>parse_date(loan['due']):
                                    store.renewal(account,loan,now,'confirmed',updated['due'])
                                else:
                                    # A timeout or unchanged DOM is inconclusive: no blind click retry.
                                    store.renewal(account,loan,now,'uncertain')
                                    store.error(account,'renewal_unconfirmed')
                                    failed=True
                            except Exception:
                                store.renewal(account,loan,now,'uncertain')
                                raise LibraryError('renewal_unconfirmed') from None
                    # Surface unresolved operations even after a later successful page read.
                    for pending in store.db.execute("SELECT * FROM renewals WHERE account=? AND status IN ('pending','uncertain')",(account.id,)).fetchall():
                        observed=next((l for l in json.loads(store.db.execute('SELECT loans FROM accounts WHERE id=?',(account.id,)).fetchone()[0]) if l['id']==pending['loan']),None)
                        if observed and parse_date(observed['due'])>parse_date(pending['old_due']):
                            store.renewal(account,{'id':pending['loan'],'due':pending['old_due']},now,'confirmed',observed['due'])
                        else:
                            store.error(account,'renewal_unconfirmed')
                            failed=True
                    # Reservation reading is independent; failure cannot discard confirmed loans.
                    if hasattr(session,'reservations'):
                        try:
                            reservations=session.reservations()
                        except Exception:
                            reservations={'status':'unverified','items':[]}
                        with store.db:
                            store.db.execute('UPDATE accounts SET reservations=? WHERE id=?',
                                             (json.dumps(reservations,ensure_ascii=False),account.id))
                except Exception as exc:
                    failed=True
                    store.error(account,exc.args[0] if isinstance(exc,LibraryError) else 'portal_unavailable')
                finally:
                    if session:
                        try: session.close()
                        except Exception: pass
            with store.db:
                store.db.execute('UPDATE runs SET finished=?,status=? WHERE id=?',
                                 (clock().isoformat(),'partial' if failed else 'ok',run_id))
            return store.view()
        finally:
            store.close()


def summary(view, now=None):
    now=now or clock()
    lines=['📚 Biblioteca — '+now.strftime('%d/%m/%Y')]
    for account in view['accounts']:
        lines.extend(['',account['name']])
        if account['error']:
            lines.append('⚠️ Revisión con incidencia; comprobar los préstamos en el portal.')
        if account['checked']:
            checked=datetime.fromisoformat(account['checked']).astimezone(ZONE)
            lines.append('Última consulta: '+checked.strftime('%d/%m/%Y %H:%M'))
        if account['loans'] is None:
            lines.append('No se ha podido confirmar el listado de préstamos.')
            continue
        if not account['loans']:
            lines.append('Sin préstamos activos en la última consulta correcta.')
        for loan in sorted(account['loans'],key=lambda x:(x['due'],x['title'])):
            day=parse_date(loan['due'])
            suffix=' — VENCIDO' if day<now.date() else (' — vence hoy' if day==now.date() else '')
            lines.append('• '+loan['title']+' — devolver '+day.strftime('%d/%m/%Y')+suffix)
    if not view['accounts']:
        lines.append('Todavía no hay cuentas consultadas.')
    return '\n'.join(lines)


def chunks(text, limit=3500):
    # Plain text, including long titles: never leave oversized lines or broken entities.
    result=[]
    while text:
        cut=len(text) if len(text)<=limit else text.rfind('\n',0,limit+1)
        if cut<=0: cut=min(limit,len(text))
        result.append(text[:cut])
        text=text[cut:].lstrip('\n')
    return result


def sunday_delivery(root, text, send, *, now_fn=clock):
    """At most one send attempt per part/week. Unknown delivery is never blindly retried."""
    now=now_fn()
    if now.weekday()!=6:
        return {'status':'outside_sunday','sent_parts':0}
    with exclusive(root):
        store=Store(root)
        sent=0
        try:
            week=now.date().isoformat()
            if store.db.execute('SELECT 1 FROM deliveries WHERE week=? LIMIT 1',(week,)).fetchone():
                return {'status':'already_attempted','sent_parts':0}
            parts=chunks(text)
            for part,body in enumerate(parts):
                store.claim_delivery(week,part,body,now)
            for part,body in enumerate(parts):
                current=now_fn()
                if current.date()!=now.date() or current.weekday()!=6 or (current.hour,current.minute)==(23,59):
                    return {'status':'window_closed','sent_parts':sent}
                try:
                    send(body)
                except Exception:
                    store.delivery_result(week,part,'unknown')
                    return {'status':'unknown','sent_parts':sent}
                store.delivery_result(week,part,'sent')
                sent+=1
            return {'status':'attempted','sent_parts':sent}
        finally:
            store.close()
