"""Herramientas acotadas al perfil MARK. El modelo nunca recibe secretos."""
import json
import subprocess
import os
import signal
from .core import Store, LibraryError, summary


def home():
    from hermes_cli.config import get_hermes_home
    value=get_hermes_home().resolve()
    if value.name!='mark' or value.parent.name!='profiles':
        raise LibraryError('profile_scope')
    return value


def handle(args, **kwargs):
    try:
        if set(args)-{'action'} or args.get('action') not in ('estado','consultar'):
            raise LibraryError('invalid_action')
        root=home()
        # Both conversational actions read the same persisted table. Browser work
        # belongs exclusively to the deterministic scheduled/admin entry points.
        store=Store(root/'biblioteca-data')
        try: result=store.view()
        finally: store.close()
        result['source']='local_table'
        result['summary']=summary(result)
        return json.dumps(result,ensure_ascii=False)
    except Exception as exc:
        return json.dumps({'error':exc.args[0] if isinstance(exc,LibraryError) else 'operation_failed',
                           'instruction':'No afirmar datos comprobados ni actualización reciente. No pedir contraseñas por chat.'},ensure_ascii=False)


def run_worker(profile, *, renew=False):
    process=None
    try:
        process=subprocess.Popen([str(profile/'biblioteca-venv/bin/python'),str(profile/'biblioteca-worker.py'),
                                  'renew' if renew else 'read'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                  text=True,start_new_session=True)
        try:
            stdout,_=process.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGTERM)
            try: process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.communicate()
            raise LibraryError('worker_timeout') from None
        result=json.loads(stdout)
        if process.returncode or 'accounts' not in result:
            raise LibraryError('worker_failed')
        return result
    except LibraryError:
        raise
    except Exception:
        raise LibraryError('worker_failed') from None


def register(ctx):
    description=('Biblioteca de MARK: estado y consultar leen exclusivamente la tabla local de préstamos, fechas y reservas existentes. '
                 'No acceden a Internet ni renuevan. Indica la fecha de última actualización de los datos; '
                 'las revisiones automáticas mantienen la tabla. Una lectura fallida no significa cero libros.')
    schema={'name':'mark_biblioteca','description':description,'parameters':{
        'type':'object','properties':{'action':{'type':'string','enum':['estado','consultar']}},
        'required':['action'],'additionalProperties':False}}
    ctx.register_tool(name='mark_biblioteca',toolset='mark_biblioteca',schema=schema,
                      handler=handle,description=description)
