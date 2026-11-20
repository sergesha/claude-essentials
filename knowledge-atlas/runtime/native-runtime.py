#!/usr/bin/env python3
"""Run the qualified local TerminusDB/VectorLink runtime in the foreground."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import tomllib

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime',required=True,type=Path,help='Prepared runtime tree: terminusdb/, native-tools/swipl/')
parser.add_argument('--state',required=True,type=Path,help='Own native db/ and vectorlink/ state directory')
parser.add_argument('--profile',required=True,type=Path)
parser.add_argument('--tokenizer',required=True,type=Path)
parser.add_argument('--provider',required=True,help='Explicit-role embedding bridge URL')
parser.add_argument('--port',required=True,type=int)
parser.add_argument('command',choices=('init','serve'))
args=parser.parse_args()
runtime=args.runtime.resolve();repo=runtime/'terminusdb';state=args.state.resolve()
profile=json.loads(args.profile.read_text());tokenizer=args.tokenizer.resolve()
canonical=json.dumps(profile['profile'],sort_keys=True,separators=(',',':'),ensure_ascii=False)
if hashlib.sha256(canonical.encode()).hexdigest()!=profile['profile_id']:
    parser.error('profile digest mismatch')
if hashlib.sha256(tokenizer.read_bytes()).hexdigest()!=profile['profile']['endpoint']['tokenizer_sha256']:
    parser.error('tokenizer does not match profile')
algorithm=profile['profile']['plugin_profile']['index_algorithm']
if algorithm!='FLAT':
    parser.error('qualified runtime requires the FLAT search profile')
lock=tomllib.loads((repo/'src/rust/Cargo.lock').read_text())
versions=[package['version'] for package in lock['package'] if package['name']=='vectorlink']
if not versions or any(tuple(int(n) for n in version.split('-')[0].split('.'))<(2,0,0) for version in versions):
    parser.error('VectorLink >=2.0.0 required in the prepared native build')
if not 1<=args.port<=65535:parser.error('port outside valid range')
if args.command=='serve' and not (state/'db').is_dir():
    parser.error('native database is not initialized; use init')
os.umask(0o077)
state.mkdir(parents=True,exist_ok=True)
environment=os.environ.copy()
swi=runtime/'native-tools/swipl'
environment.update({
    'PATH':str(swi/'bin')+os.pathsep+environment.get('PATH',''),
    'SWIPL_PACK_PATH':str(swi/'lib/swipl/pack'),
    'TERMINUSDB_SERVER_DB_PATH':str(state/'db'),
    'TERMINUSDB_SERVER_PORT':str(args.port),
    'TERMINUSDB_LISTEN_ADDRESS':'127.0.0.1',
    'TERMINUSDB_BUILTIN_PLUGINS_PATH':str(repo/'builtin_plugins'),
    'TERMINUSDB_INDEXER_BACKEND':'vectorlink',
    'VECTORLINK_EMBED_PROVIDER':'generic_http',
    'VECTORLINK_EMBED_URL':args.provider.rstrip('/'),
    'VECTORLINK_MODEL':profile['model_namespace'],
    'VECTORLINK_DIM':'256',
    'VECTORLINK_SEARCH_ALGORITHM':algorithm,
    'VECTORLINK_TOKENIZER_PATH':str(tokenizer),
    'VECTORLINK_DATA_DIR':str(state/'vectorlink'),
})
# Prepare the key with the graph offline. The pinned TerminusDB CLI accepts keys
# as arguments, so pass its arguments inside the saved Prolog state via stdin.
# The real key never appears in an OS command line, environment or native log.
key_file = state / 'admin.key'
with (state / '.credentials.lock').open('a') as lock_file:
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    if args.command == 'init' and (state / 'db').exists():
        parser.error('native database already exists; use serve')
    if args.command == 'init' or not key_file.is_file():
        # A legacy database already contains its system and publication graphs.
        # Only change its admin password. Retrying after an interrupted rotation
        # also takes this path until the new key is durably published.
        command = ("[store,init,'--key',Key]" if args.command == 'init' else
                   "[user,password,admin,'--password',Key]")
        goal = ("read_line_to_string(user_input,S),atom_string(Key,S),"
                f"set_prolog_flag(argv,{command}),cli:cli_toplevel")
        secret = secrets.token_urlsafe(32)
        with tempfile.NamedTemporaryFile(dir=state, delete=False) as pending:
            pending.write(secret.encode() + b'\n')
            pending.flush()
            os.fsync(pending.fileno())
        try:
            result = subprocess.run(
                [swi / 'bin/swipl', '-x', repo / 'terminusdb', '-g', goal, '-t', 'halt(1)'],
                cwd=repo, env=environment, input=secret + '\n', text=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                parser.exit(1, 'failed to configure the native graph credential; graph remains offline\n')
            os.replace(pending.name, key_file)
        finally:
            Path(pending.name).unlink(missing_ok=True)
if args.command == 'serve':
    os.chdir(repo)
    os.execve(str(repo/'terminusdb'), [str(repo/'terminusdb'), 'serve'], environment)
