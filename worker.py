"""Proceso de navegador en venv propio; no modifica dependencias de Hermes."""
import json
from pathlib import Path
import sys
import importlib.util


def main():
    profile=Path(__file__).resolve().parent
    if profile.name!='mark' or profile.parent.name!='profiles': return 1
    spec=importlib.util.spec_from_file_location('mark_worker_plugin',profile/'plugins/mark-biblioteca/__init__.py',
        submodule_search_locations=[str(profile/'plugins/mark-biblioteca')])
    mod=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=mod
    spec.loader.exec_module(mod)
    from mark_worker_plugin.core import load_accounts, run
    from mark_worker_plugin.web import PortalSession
    if len(sys.argv)!=2 or sys.argv[1] not in ('read','renew','validate-accounts'): return 1
    try:
        if sys.argv[1]=='validate-accounts':
            import tempfile
            with tempfile.TemporaryDirectory(prefix='biblioteca-validation-',dir=profile) as folder:
                view=run(Path(folder),load_accounts(profile/'biblioteca-secrets/candidate.json'),PortalSession)
        else:
            view=run(profile/'biblioteca-data',load_accounts(profile/'biblioteca-secrets/credenciales.json'),
                     PortalSession,renew=sys.argv[1]=='renew')
        print(json.dumps(view,ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({'error':'worker_failed'}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
