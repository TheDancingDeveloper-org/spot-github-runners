"""Durable whole-run custody; no uploaded code, timer reclamation or deletion.

Callers must hold the existing global build lock before entering this store.
Acquire/release require EX; stage and route checks retain SH until completion.
This module supplies custody only. Production control/transfer enrollment must
be completed before enabling acquisition on a real host.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
import urllib.request

ROOT = Path('/mnt/runner-data/remote-build/qualification-reservations')
CONFIG = Path('/etc/oxidex-remote-build.json')
LIMIT = 65536
COMMANDS = frozenset({'prepare', 'upload', 'verify-source', 'bootstrap',
                      'execute', 'pack', 'replay', 'cleanup', 'upload-receive',
                      'task20-bootstrap','task20-execute','task20-pack','task20-replay'})
FIELDS = {'schema', 'project', 'attempt', 'expected_vm_id', 'expected_boot',
          'head', 'tree', 'bundle_sha256', 'attestation_sha256',
          'attestation_namespace', 'stages_sha256'}


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate reservation JSON key')
        result[key] = value
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_actor(actor):
    if not isinstance(actor, dict) or set(actor) != {'pid', 'start'} or any(type(actor[key]) is not int or actor[key] <= 0 for key in actor):
        raise ValueError('Actual PID and start tick identity required')


def command_arguments(command, argv, session):
    """Current fixed host allowlist; Task20 IDs require authenticated additions."""
    if not isinstance(argv, list) or any(not isinstance(arg, str) for arg in argv):
        raise ValueError('Fixed stage argument list required')
    head = session['head']; project = session['project']; bundle = session['bundle_sha256']
    if session.get('profile')=='task20-v1':
        identity=['--round',session['attempt'],'--expected-head',head,'--run-id',project]
        base=['python3','/target/checkout/tools/remote-build/']
        fixed20={'prepare':['prepare'],'upload-receive':['upload-receive'],'cleanup':['cleanup'],
            'verify-source':['verify-source','--head',head,'--bundle-sha256',bundle,'--run-id',project],
            'task20-bootstrap':['python3',base[1]+'task20_bootstrap.py'],
            'task20-execute':['python3',base[1]+'task20_worker.py',*identity],
            'task20-pack':['python3',base[1]+'task20_results.py','--pack',*identity],
            'task20-replay':['python3',base[1]+'task20_results.py','--replay',*identity]}
        if command in fixed20 and argv==fixed20[command]:return
        raise ValueError('Task20 command differs from fixed profile arguments')
    output = '/target/ops/evidence/remote-qualification/'+project
    base = ['python3', '/target/checkout/tools/remote-build/qualification.py']
    fixed = {
        'prepare': ['prepare'], 'upload': ['upload'], 'cleanup': ['cleanup'],
        'verify-source': ['verify-source', '--head', head, '--bundle-sha256', bundle, '--run-id', project],
        'execute': base+['--remote-run', '--output', output, '--expected-head', head],
        'pack': base+['--pack-results', '--output', output, '--expected-head', head, '--result-file', '/target/pack-result.json'],
        'replay': base+['--verify-results-tar', '/target/results.tar.gz', '--output', output, '--expected-head', head, '--result-file', '/target/replay-result.json'],
    }
    if command == 'bootstrap':
        prefix = ['python3', '/src/qualification_bootstrap.py', '--head', head, '--output', output,
                  '--bundle-sha256', bundle, '--signers-sha256']
        if len(argv) == 10 and argv[:9] == prefix and re.fullmatch('[0-9a-f]{64}', argv[9]):
            return
    elif command in fixed and argv == fixed[command]:
        return
    raise ValueError('Command ID differs from fixed host arguments; unsupported Task20 command')


def record_name(filename):
    if filename != 'record.json' and not re.fullmatch('released-qualification-[0-9a-f]{12}-[0-9a-f]{32}\\.json', filename):
        raise ValueError('Fixed reservation record filename required')


def validate_session(session, stages):
    if not isinstance(session, dict) or type(session.get('schema')) is not int or session['schema'] not in (1,2):
        raise ValueError('Unknown reservation session schema')
    expected=FIELDS if session['schema']==1 else FIELDS|{'profile'}
    if set(session)!=expected or (session['schema']==2 and session['profile']!='task20-v1'):
        raise ValueError('Unknown root-controlled reservation profile')
    for key, size in (('head', 40), ('tree', 40), ('bundle_sha256', 64),
                      ('attestation_sha256', 64), ('stages_sha256', 64)):
        if not isinstance(session[key], str) or not re.fullmatch('[0-9a-f]{'+str(size)+'}', session[key]):
            raise ValueError('Malformed reservation source identity')
    if (not isinstance(session['project'], str) or not re.fullmatch(
            'qualification-'+session['head'][:12]+'-[0-9a-f]{32}', session['project'])
            or not isinstance(session['attempt'], str) or not re.fullmatch('final-r[1-9][0-9]{0,5}', session['attempt'])
            or not isinstance(session['expected_vm_id'], str) or not re.fullmatch('[0-9]{1,20}', session['expected_vm_id'])
            or not isinstance(session['expected_boot'], str) or not re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', session['expected_boot'])
            or session['attestation_namespace'] != 'oxidex-qualification-source-v1'):
        raise ValueError('Malformed reservation host/run identity')
    if not isinstance(stages, list) or not 1 <= len(stages) <= 64:
        raise ValueError('Bounded ordered stages required')
    for ordinal, stage in enumerate(stages):
        if (not isinstance(stage, dict) or set(stage) != {'ordinal', 'command', 'argv_sha256'}
                or type(stage['ordinal']) is not int or stage['ordinal'] != ordinal
                or not isinstance(stage['command'], str) or stage['command'] not in COMMANDS or not isinstance(stage['argv_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', stage['argv_sha256'])):
            raise ValueError('Unknown stage descriptor')
    names = [stage['command'] for stage in stages]
    if session['schema']==2:
        if names!=['prepare','upload-receive','verify-source','task20-bootstrap','task20-execute','task20-pack','task20-replay','cleanup']:
            raise ValueError('Exactly eight Task20 profile ordinals required')
    elif names[:3] != ['prepare', 'upload', 'verify-source'] or names[-1] != 'cleanup':
        raise ValueError('Source verification must precede execution and cleanup must finish')
    if any(command in names[3:] for command in ('prepare', 'upload', 'verify-source')):
        raise ValueError('Source admission cannot be replayed')
    if digest(stages) != session['stages_sha256']:
        raise ValueError('Root-reviewed stage descriptor differs')


def authenticate_claim(document, signature, session):
    """Verify detached control bytes with the existing root source trust anchor."""
    import tempfile
    from .host_gate import preauthenticate
    if not isinstance(document, bytes) or not isinstance(signature, bytes) or len(document) > LIMIT or len(signature) > 16384:
        raise ValueError('Bounded detached source document and signature required')
    if hashlib.sha256(document).hexdigest() != session['attestation_sha256']:
        raise ValueError('Source claim document digest differs')
    facts = json.loads(document, object_pairs_hook=pairs)
    if not isinstance(facts, dict) or any(facts.get(key) != session[key] for key in ('head', 'tree', 'bundle_sha256')) or facts.get('run_id') != session['project']:
        raise ValueError('Detached source claim binding differs')
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)
        (path/'document.json').write_bytes(document)
        (path/'document.sig').write_bytes(signature)
        preauthenticate(path, session['head'], session['bundle_sha256'], session['project'])
    return {**facts, 'attestation_sha256': session['attestation_sha256']}


class Store:
    def __init__(self, root=ROOT, *, owner_uid=0):
        self.root = Path(root)
        self.owner_uid = owner_uid

    @contextmanager
    def locked(self):
        # Production startup owns creation. Never repair untrusted ancestors.
        for path in (self.root, *self.root.parents):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, self.owner_uid) or info.st_mode & 0o022:
                raise ValueError('Untrusted reservation directory')
        fd = os.open(self.root/'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid or info.st_nlink != 1 or info.st_mode & 0o077:
                raise ValueError('Untrusted reservation lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def read(self, filename='record.json'):
        record_name(filename)
        try:
            fd = os.open(self.root/filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > LIMIT:
                raise ValueError('Untrusted reservation record')
            raw = os.read(fd, LIMIT+1)
            if len(raw) != info.st_size:
                raise ValueError('Reservation record changed or oversized')
            record = json.loads(raw, object_pairs_hook=pairs)
            if not isinstance(record, dict) or set(record) != {'schema', 'reservation_id', 'session', 'stages', 'host', 'owner', 'state', 'next_ordinal', 'active', 'events','descriptor_sha256'} or type(record.get('schema')) is not int or record['schema'] != 1:
                raise ValueError('Unknown reservation record schema')
            validate_session(record['session'], record['stages'])
            descriptor=record['descriptor_sha256']
            if descriptor is not None and (not isinstance(descriptor,str) or not re.fullmatch('[0-9a-f]{64}',descriptor)):
                raise ValueError('Malformed root descriptor custody digest')
            if record['session'].get('profile')=='task20-v1' and descriptor is None:
                raise ValueError('Actual Task20 reviewed descriptor custody required')
            if (not isinstance(record['reservation_id'], str) or not re.fullmatch('[0-9a-f]{32}', record['reservation_id'])
                    or record['host'] != [record['session']['expected_vm_id'], record['session']['expected_boot']]
                    or record['state'] not in ('CLAIMED', 'SOURCE_VERIFIED', 'HELD', 'RECOVERING', 'RELEASED')
                    or type(record['next_ordinal']) is not int or not 0 <= record['next_ordinal'] <= len(record['stages'])
                    or not isinstance(record['events'], list) or len(record['events']) > 128):
                raise ValueError('Malformed reservation custody')
            validate_actor(record['owner'])
            for event in record['events']:
                required = {'ordinal', 'actor', 'command', 'exit_code', 'log_sha256'}
                if not isinstance(event, dict) or not required <= set(event) or set(event)-required-{'disposition', 'reason'}:
                    raise ValueError('Unknown reservation event schema')
                ordinal = event['ordinal']
                if type(ordinal) is not int or not 0 <= ordinal < len(record['stages']) or event['command'] != record['stages'][ordinal]['command']:
                    raise ValueError('Terminal event stage differs')
                validate_actor(event['actor'])
                if event.get('disposition') not in (None, 'INTERRUPTED', 'FINISH_REFUSED'):
                    raise ValueError('Unknown event disposition')
                if event['exit_code'] is None:
                    if event.get('disposition') not in ('INTERRUPTED', 'FINISH_REFUSED'):
                        raise ValueError('Unknown exit requires explicit interruption evidence')
                elif type(event['exit_code']) is not int:
                    raise ValueError('Actual integer terminal status required')
                if event['log_sha256'] is None:
                    if event.get('disposition') not in ('INTERRUPTED', 'FINISH_REFUSED'):
                        raise ValueError('Terminal log digest missing')
                elif not isinstance(event['log_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', event['log_sha256']):
                    raise ValueError('Malformed terminal log digest')
                if 'reason' in event and (not isinstance(event['reason'], str) or len(event['reason']) > 200):
                    raise ValueError('Malformed retained failure reason')
            if record['active'] is not None:
                if not isinstance(record['active'], dict) or set(record['active']) != {'ordinal', 'actor', 'command'}:
                    raise ValueError('Malformed active stage')
                validate_actor(record['active']['actor'])
                if record['next_ordinal'] >= len(record['stages']) or type(record['active']['ordinal']) is not int or record['active']['ordinal'] != record['next_ordinal'] or record['active']['command'] != record['stages'][record['next_ordinal']]['command']:
                    raise ValueError('Active stage ordinal differs')
            if record['state'] == 'RELEASED' and (record['active'] is not None or record['next_ordinal'] != len(record['stages']) or not record['events'] or record['events'][-1]['ordinal'] != len(record['stages'])-1 or record['events'][-1]['command'] != 'cleanup' or type(record['events'][-1]['exit_code']) is not int or record['events'][-1]['exit_code'] != 0 or record['events'][-1].get('disposition') is not None):
                raise ValueError('Release tombstone lacks terminal cleanup')
            return record
        finally:
            os.close(fd)

    def write(self, record, filename='record.json'):
        record_name(filename)
        if not isinstance(record.get('events'), list) or len(record['events']) > 128:
            raise ValueError('Reservation event budget exceeded before publication')
        raw = canonical(record)
        if len(raw) > LIMIT:
            raise ValueError('Reservation record bound exceeded')
        temporary = self.root/('.record-'+uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root/filename)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def acquire(self, session, stages, *, host, owner, authenticate, quiescent,descriptor_sha256=None):
        validate_session(session, stages)
        validate_actor(owner)
        if session.get('profile')=='task20-v1' and (not isinstance(descriptor_sha256,str) or not re.fullmatch('[0-9a-f]{64}',descriptor_sha256)):
            raise ValueError('Root Task20 descriptor digest required')
        if self.root == ROOT and enrollment() is None:
            raise ValueError('Production reservation requires explicit mounted-storage enrollment')
        if host != (session['expected_vm_id'], session['expected_boot']):
            raise ValueError('Actual host differs from reservation claim')
        # authenticate verifies actual detached bytes/signature, never a boolean
        # from a request. quiescent must perform complete host observations.
        facts = authenticate(session)
        if not isinstance(facts, dict) or any(facts.get(key) != session[key] for key in
                ('head', 'tree', 'bundle_sha256', 'attestation_sha256')) or facts.get('run_id') != session['project']:
            raise ValueError('Detached signed source claim differs')
        with self.locked():
            current = self.read()
            if current is not None:
                if current['state'] != 'RELEASED' or current['session']['project'] == session['project']:
                    raise ValueError('Reservation or same-run release tombstone exists; inspect only')
                archive = 'released-'+current['session']['project']+'.json'
                previous = self.read(archive)
                if previous is not None and previous != current:
                    raise ValueError('Release tombstone differs; custody held')
                if previous is None:
                    self.write(current, archive)
            if self.read('released-'+session['project']+'.json') is not None:
                raise ValueError('Released run cannot be reacquired')
            if quiescent() is not True:
                raise ValueError('Complete actual quiescence required')
            record = dict(schema=1, reservation_id=uuid.uuid4().hex, session=session,
                          stages=stages, host=list(host), owner=owner, state='CLAIMED',
                          next_ordinal=0, active=None, events=[],descriptor_sha256=descriptor_sha256)
            self.write(record)
            return record

    def begin(self, identity, ordinal, *, host, actor, argv):
        validate_actor(actor)
        with self.locked():
            record = self.read()
            self.match(record, identity, host)
            if type(ordinal) is not int or not 0 <= ordinal < len(record['stages']) or record['state'] in ('HELD', 'RELEASED') or record['active'] is not None or ordinal != record['next_ordinal']:
                raise ValueError('Stage replay, gap or uncertain prior stage')
            stage = record['stages'][ordinal]
            command_arguments(stage['command'], argv, record['session'])
            if digest(argv) != stage['argv_sha256']:
                raise ValueError('Stage argv differs from reviewed command')
            if stage['command'] not in ('prepare', 'upload', 'upload-receive', 'verify-source', 'cleanup') and record['state'] != 'SOURCE_VERIFIED':
                raise ValueError('Actual source verification required before execution')
            record['active'] = dict(ordinal=ordinal, actor=actor, command=stage['command'])
            self.write(record)
            return record

    def finish(self, identity, ordinal, *, host, actor, exit_code, log_sha256, quiescent, binding=None):
        with self.locked():
            record = self.read()
            self.match(record, identity, host)
            active = record['active']
            if type(ordinal) is not int or active is None or active['actor'] != actor or active['ordinal'] != ordinal:
                raise ValueError('Exact active stage owner required')
            refused = any(event.get('disposition') == 'FINISH_REFUSED' and event['ordinal'] == ordinal
                          for event in record['events'])
            # A prior refusal is terminal for that actor. Only an explicit
            # dead-actor cleanup transition may finish under a new actor.
            recovered = (record['state'] == 'RECOVERING' and record['events']
                         and record['events'][-1].get('disposition') == 'INTERRUPTED'
                         and record['events'][-1]['ordinal'] == ordinal
                         and record['events'][-1]['actor'] != actor)
            if (refused and not recovered) or record['state'] == 'HELD':
                raise ValueError('Refused stage requires explicit recovery, not finalization retry')
            if type(exit_code) is not int or not isinstance(log_sha256, str) or not re.fullmatch('[0-9a-f]{64}', log_sha256):
                raise ValueError('Actual terminal status and log digest required')
            try:
                if quiescent() is not True:
                    raise ValueError('Complete actual stage quiescence required')
                if active['command'] == 'verify-source' and exit_code == 0:
                    expected = record['session']
                    if not isinstance(binding, dict) or any(binding.get(key) != expected[key] for key in ('head', 'tree', 'bundle_sha256', 'attestation_sha256')):
                        raise ValueError('Actual sealed source differs from reserved claim')
                    record['state'] = 'SOURCE_VERIFIED'
            except Exception as error:
                record['state'] = 'HELD'
                if len(record['events']) < 126:
                    record['events'].append({**active, 'exit_code': None, 'log_sha256': log_sha256,
                                             'disposition': 'FINISH_REFUSED', 'reason': str(error)[:200]})
                self.write(record)
                raise
            if exit_code != 0:
                record['state'] = 'HELD'
            record['events'].append({**active, 'exit_code': exit_code, 'log_sha256': log_sha256})
            record['active'] = None
            record['next_ordinal'] += 1
            self.write(record)
            return record

    def release(self, identity, *, host, quiescent, project=None):
        with self.locked():
            record = self.read()
            if (record is None or record['reservation_id'] != identity) and project is not None:
                if not isinstance(project, str) or not re.fullmatch('qualification-[0-9a-f]{12}-[0-9a-f]{32}', project):
                    raise ValueError('Exact archived run namespace required')
                record = self.read('released-'+project+'.json')
                if record is None or record['state'] != 'RELEASED' or record['session']['project'] != project:
                    raise ValueError('Exact released tombstone unavailable')
            self.match(record, identity, host)
            if record['state'] == 'RELEASED':
                return record  # durable lost-ack answer, no repeated effects
            if (record['state'] not in ('SOURCE_VERIFIED', 'RECOVERING') or record['active'] is not None
                    or record['next_ordinal'] != len(record['stages'])
                    or not record['events'] or record['events'][-1]['command'] != 'cleanup'
                    or record['events'][-1]['exit_code'] != 0):
                raise ValueError('Complete same-run terminal and cleanup required')
            if quiescent() is not True:
                raise ValueError('Complete actual release quiescence required')
            record['state'] = 'RELEASED'
            self.write(record)  # custody only; never delete source/cache/evidence
            return record

    def cleanup_after_failure(self, identity, *, host, actor, argv, quiescent):
        """Explicit root recovery; never report an uncertain stage as successful."""
        validate_actor(actor)
        with self.locked():
            record = self.read()
            self.match(record, identity, host)
            if record['state'] == 'RELEASED' or quiescent() is not True:
                raise ValueError('Actual same-run child/transfer quiescence required')
            ordinal = len(record['stages'])-1
            stage = record['stages'][ordinal]
            if record['next_ordinal'] >= len(record['stages']) or digest(argv) != stage['argv_sha256']:
                raise ValueError('Owned cleanup already finished or command differs')
            if record['active'] is not None and record['active']['command'] == 'cleanup':
                if (record['state'] not in ('SOURCE_VERIFIED', 'HELD', 'RECOVERING')
                        or record['active']['ordinal'] != ordinal or record['next_ordinal'] != ordinal):
                    raise ValueError('Uncertain cleanup state or ordinal differs')
                previous = record['active']['actor']
                if previous == actor or not actor_exited(previous):
                    raise ValueError('Prior cleanup actor remains or cannot be excluded')
                if len(record['events']) >= 126:
                    raise ValueError('Recovery event budget exhausted')
                record['events'].append({**record['active'], 'exit_code': None,
                                         'log_sha256': None, 'disposition': 'INTERRUPTED'})
                record['state'] = 'RECOVERING'
                record['active'] = dict(ordinal=ordinal, actor=actor, command='cleanup')
                self.write(record)
                return record
            if record['active'] is not None:
                record['events'].append({**record['active'], 'exit_code': None,
                                         'log_sha256': None, 'disposition': 'INTERRUPTED'})
            record['state'] = 'RECOVERING'
            record['next_ordinal'] = ordinal
            record['active'] = dict(ordinal=ordinal, actor=actor, command='cleanup')
            self.write(record)
            return record

    @staticmethod
    def match(record, identity, host):
        if record is None or record.get('reservation_id') != identity or record.get('host') != list(host):
            raise ValueError('Reservation/host identity differs')

    def route(self, project, *, host, actor_matches):
        with self.locked():
            record = self.read()
            self.fence_released(project, record)
            if record is None or record['state'] == 'RELEASED':
                return
            if record['host'] != list(host) or record['session']['project'] != project or record['active'] is None or record['state'] == 'HELD':
                raise ValueError('Host reserved; no matching active stage')
            if not actor_matches(record['active']['actor']):
                raise ValueError('Host reserved for another actual stage process')

    def fence_released(self, project, record):
        if project is None or not isinstance(project, str) or not project.startswith('qualification-'):
            return
        if not re.fullmatch('qualification-[0-9a-f]{12}-[0-9a-f]{32}', project):
            raise ValueError('Malformed qualification namespace')
        if record is not None and record['state'] == 'RELEASED' and record['session']['project'] == project:
            raise ValueError('Released run cannot execute or delete preserved source')
        archived = self.read('released-'+project+'.json')
        if archived is not None:
            if archived['state'] != 'RELEASED' or archived['session']['project'] != project:
                raise ValueError('Malformed namespace tombstone')
            raise ValueError('Archived released run cannot execute or clean up')


def process(pid):
    """Read Linux identity; exited/zombie/unknown is never a stage owner."""
    raw = Path('/proc/'+str(pid)+'/stat').read_text()
    fields = raw[raw.rfind(')')+2:].split()
    if fields[0] in ('Z', 'X'):
        raise ValueError('Stage owner exited')
    return {'pid': pid, 'start': int(fields[19])}, int(fields[1])


def current_actor():
    return process(os.getpid())[0]


def actor_exited(actor):
    """Only a missing, zombie, or reused Linux PID excludes the recorded actor."""
    validate_actor(actor)
    try:
        observed, _ = process(actor['pid'])
    except FileNotFoundError:
        return True
    except ValueError as error:
        if str(error) == 'Stage owner exited':
            return True
        raise
    return observed != actor


def ancestor_matches(actor, pid):
    validate_actor(actor)
    chain = []
    for _ in range(64):
        if pid <= 0:
            return False
        observed, parent = process(pid)
        if chain and observed['start'] > chain[-1][0]['start']:
            raise ValueError('Parent started after child; PID reuse or invalid ancestry')
        chain.append((observed, parent))
        if observed == actor:
            status = Path('/proc/'+str(pid)+'/status').read_text()
            uid = next(line for line in status.splitlines() if line.startswith('Uid:')).split()[1:]
            if uid != ['0', '0', '0', '0']:
                return False
            # Recheck complete adjacent links after reaching the authorized root.
            for index, (selected, selected_parent) in enumerate(chain):
                if process(selected['pid']) != (selected, selected_parent):
                    raise ValueError('Ancestry changed before grant')
                if index+1 < len(chain) and selected_parent != chain[index+1][0]['pid']:
                    raise ValueError('Ancestry link differs')
            return True
        if process(pid) != (observed, parent):
            raise ValueError('Stage ancestry changed during observation')
        pid = parent
    raise ValueError('Stage ancestry bound exceeded')


def validate_enrollment(policy):
    """Require stable disk identity; enabled boot-local st_dev records need migration."""
    if not isinstance(policy, dict) or type(policy.get('schema')) is not int or type(policy.get('enabled')) is not bool:
        raise ValueError('Unknown reservation enrollment')
    if policy['schema'] == 1:
        if (set(policy) != {'schema', 'enabled', 'uuid', 'device'}
                or policy['enabled'] or policy['uuid'] is not None or policy['device'] is not None):
            raise ValueError('Legacy device-number enrollment requires reviewed migration')
        return None
    if policy['schema'] != 2 or set(policy) != {'schema', 'enabled', 'uuid', 'by_id', 'serial'}:
        raise ValueError('Unknown reservation enrollment')
    if not policy['enabled']:
        if any(policy[key] is not None for key in ('uuid', 'by_id', 'serial')):
            raise ValueError('Unenrolled policy has unexpected storage identity')
        return None
    try:
        canonical_uuid = str(uuid.UUID(policy['uuid'])) == policy['uuid']
    except (ValueError, AttributeError, TypeError):
        canonical_uuid = False
    if (not canonical_uuid or not isinstance(policy['by_id'], str)
            or not re.fullmatch(r'/dev/disk/by-id/google-[a-z][a-z0-9-]{0,62}', policy['by_id'])
            or not isinstance(policy['serial'], str) or not 1 <= len(policy['serial']) <= 256
            or any(ord(char) < 33 or ord(char) > 126 for char in policy['serial'])):
        raise ValueError('Stable enrolled storage identity missing')
    return policy


def storage_matches(policy, observed):
    """Compare cross-boot stable identity; st_dev remains a current-boot fact."""
    if (not isinstance(observed, dict) or set(observed) != {'uuid', 'by_id', 'serial', 'mount_device'}
            or type(observed['mount_device']) is not int or observed['mount_device'] <= 0
            or any(observed[key] != policy[key] for key in ('uuid', 'by_id', 'serial'))):
        raise ValueError('Enrolled cache mount unavailable or replaced; custody held')
    return observed


def enrollment():
    """Read trusted boot configuration; absent enrolled cache is not idleness."""
    fd = os.open(CONFIG, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > 16384:
            raise ValueError('Untrusted builder enrollment configuration')
        config = json.loads(os.read(fd, 16385), object_pairs_hook=pairs)
    finally:
        os.close(fd)
    legacy = {'role', 'cpus', 'memory_gib', 'image', 'approved_image_id'}
    if (isinstance(config, dict) and set(config) == legacy and config.get('role') == 'builder'
            and type(config.get('cpus')) is int and config['cpus'] == 16
            and type(config.get('memory_gib')) is int and config['memory_gib'] == 28
            and config.get('image') == 'oxidex-remote-builder:1.99.0'
            and (config.get('approved_image_id') is None or isinstance(config['approved_image_id'], str)
                 and re.fullmatch('sha256:[0-9a-f]{64}', config['approved_image_id']))):
        return None  # Explicitly recognized historical configuration schema.
    policy = validate_enrollment(config.get('reservation_enrollment') if isinstance(config, dict) else None)
    if policy is None:
        return None
    storage_matches(policy, subprocess_storage(policy['by_id']))
    return policy


def subprocess_storage(by_id):
    """Observe approved by-id block device, UUID, serial, and current mount."""
    import subprocess
    if not isinstance(by_id, str) or not re.fullmatch(r'/dev/disk/by-id/google-[a-z][a-z0-9-]{0,62}', by_id):
        raise ValueError('Approved cache by-id path required')
    device = Path(by_id).resolve(strict=True)
    if not stat.S_ISBLK(device.stat().st_mode):
        raise ValueError('Enrolled cache by-id target is not a block device')
    result = subprocess.run(['/usr/bin/findmnt', '-n', '-o', 'TARGET,SOURCE,UUID,FSTYPE', '--mountpoint', '/mnt/runner-data'],
                            check=True, capture_output=True, text=True, timeout=5)
    fields = result.stdout.split()
    if (result.stderr or len(fields) != 4 or fields[0] != '/mnt/runner-data'
            or fields[3] != 'ext4' or Path(fields[1]).resolve(strict=True) != device):
        raise ValueError('Complete expected cache mount required')
    uuid_actual = subprocess.check_output(['/usr/sbin/blkid', '-s', 'UUID', '-o', 'value', by_id],
                                          text=True, timeout=5).strip()
    serial = subprocess.check_output(['/usr/bin/lsblk', '-dn', '-o', 'SERIAL', by_id],
                                     text=True, timeout=5).strip()
    if not uuid_actual or fields[2] != uuid_actual or not serial:
        raise ValueError('Enrolled cache by-id/UUID/serial differs from mount')
    return {'uuid': uuid_actual, 'by_id': by_id, 'serial': serial,
            'mount_device': Path('/mnt/runner-data').stat().st_dev}


def host_identity():
    request = urllib.request.Request('http://169.254.169.254/computeMetadata/v1/instance/id',
                                     headers={'Metadata-Flavor': 'Google'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        if response.headers.get('Metadata-Flavor') != 'Google':
            raise ValueError('Unknown metadata identity response')
        identity = response.read(32).decode().strip()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not re.fullmatch('[0-9]{1,20}', identity) or not re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot):
        raise ValueError('Actual VM/boot identity unavailable')
    return identity, boot


def route_guard(project=None, *, pid=None, argv=None):
    """Read-side enrollment; caller retains global SH around all guarded work."""
    policy = enrollment()
    if not ROOT.exists() and not ROOT.is_symlink():
        if policy is not None:
            raise ValueError('Enrolled persistent custody directory unavailable')
        return  # Trusted explicit historical/unenrolled configuration only.
    store = Store()
    with store.locked():
        record = store.read()
        store.fence_released(project, record)
        if record is None or record['state'] == 'RELEASED':
            return
        if record['host'] != list(host_identity()) or record['active'] is None or record['state'] == 'HELD':
            raise ValueError('Host has retained or identity-mismatched whole-run custody')
        if project is not None and record['session']['project'] != project:
            raise ValueError('Host reserved for another project')
        if not ancestor_matches(record['active']['actor'], os.getpid() if pid is None else pid):
            raise ValueError('Host reserved for another actual stage process')
        if argv is not None and digest(argv) != record['stages'][record['active']['ordinal']]['argv_sha256']:
            raise ValueError('Stage command differs from root-reviewed descriptor')
        if argv is not None:
            command_arguments(record['active']['command'], argv, record['session'])
        if argv == ['cleanup']:
            raise ValueError('Reservation cleanup is custody-only; preserve source and evidence')
