"""Isolated C/D worlds using serial or bounded spawned Python processes."""
import asyncio
import concurrent.futures
import multiprocessing
from pathlib import Path
import resource
import statistics
import sys
import time

from civitas.application import economic_run
from civitas.contracts.base import content_hash, require_unique
from civitas.storage.commit_store import CommitStore, atomic_json, read_json
from .registry import record as record_attempt, run_lock


def _world(job):
    config = economic_run.parse_config(job['config'])
    root = Path(job['path'])
    start = time.perf_counter()
    cpu = time.process_time()
    if root.exists():
        identity = CommitStore(root).identity
        if identity.config_hash != job['config_hash'] or identity.run_id != job['id']:
            raise ValueError('Existing batch child has different inputs')
        result = asyncio.run(economic_run.resume_run(root))
    else:
        result = asyncio.run(economic_run.run_new(config, root, run_id=job['id']))
    store = CommitStore(root)
    records = store.verified_markers()
    _, snapshot = store.load()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    files = [p for p in root.rglob('*') if p.is_file()]
    return {'status':result['status'], 'tick':result['tick'], 'config_hash':store.identity.config_hash,
            'state_hash':result['state_hash'], 'period_business_hashes':[r.state_hash for r in records],
            'seconds':time.perf_counter()-start, 'cpu_seconds':time.process_time()-cpu,
            'rss_bytes':usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
            'artifact_files':len(files), 'artifact_bytes':sum(p.stat().st_size for p in files),
            'decisions':len(snapshot.decisions), 'provider_attempts':len(store.model_records(records[-1])),
            'external_api_calls':result['external_api_calls']}


def run_closed_batch(config, seeds, destination, *, workers=1, resume=False):
    config = economic_run.parse_config(config)
    seeds = list(seeds)
    if (not seeds or any(type(s) is not int or s < 0 for s in seeds)
            or type(workers) is not int or not 1 <= workers <= 8):
        raise ValueError('Independent worlds need distinct nonnegative seeds and 1–8 workers')
    require_unique(seeds, 'batch seed')
    if config.stopping_hazard_bps or config.cognition_mode not in ('rule','synthetic_test'):
        raise ValueError('This batch backend runs frozen offline fixed-horizon worlds')
    root = Path(destination).resolve()
    spec = {'schema_version':1,'config':config.model_dump(mode='json'),'seeds':seeds,
            'source_hash':economic_run.current_implementation()['source_hash']}
    jobs = {}
    for seed in seeds:
        raw = config.model_copy(update={'root_seed':seed}).model_dump(mode='json')
        name = f'world-{seed}'
        jobs[name] = {'id':name,'config':raw,'config_hash':content_hash(raw),'path':str(root/name)}
    if not resume:
        root.mkdir(parents=True, exist_ok=False)
        atomic_json(root/'spec.json', spec)
    elif read_json(root/'spec.json') != spec:
        raise ValueError('The frozen batch specification changed')
    started = time.perf_counter()
    report = {'schema_version':1,'workers':workers, 'backend':'serial' if workers==1 else 'spawn-process-pool',
              'assigned_worlds':len(jobs), 'expected_decisions':len(jobs)*(config.households+config.firms)*config.ticks,
              'rows': {name:{'status':'planned'} for name in jobs}, 'status':'running'}
    with run_lock(root):
        atomic_json(root/'manifest.json', report)

        def record(name, call):
            try:
                report['rows'][name] = call()
            except Exception as exc:
                report['rows'][name] = {'status':'failed','error_type':type(exc).__name__,'error':str(exc)}
            record_attempt(root, {'job_id':name, **report['rows'][name]})
            atomic_json(root/'manifest.json', report)

        if workers == 1:
            for name, job in jobs.items():
                record(name, lambda: _world(job))
        else:
            with concurrent.futures.ProcessPoolExecutor(
                    max_workers=workers, mp_context=multiprocessing.get_context('spawn')) as pool:
                pending = {pool.submit(_world, job):name for name,job in jobs.items()}
                for future in concurrent.futures.as_completed(pending):
                    record(pending[future], future.result)
        elapsed = time.perf_counter()-started
        complete = [r for r in report['rows'].values() if r['status']=='completed']
        durations = sorted(r['seconds'] for r in complete)
        decisions = sum(r['decisions'] for r in complete)
        report.update(status='completed' if len(complete)==len(jobs) else 'failed',
            completed_worlds=len(complete),wall_seconds=elapsed,decisions=decisions,
            decisions_per_second=decisions/elapsed,
            p50_world_seconds=statistics.median(durations) if durations else None,
            p95_world_seconds=durations[(95*len(durations)+99)//100-1] if durations else None,
            max_worker_rss_bytes=max((r['rss_bytes'] for r in complete), default=None),
            artifact_bytes=sum(r['artifact_bytes'] for r in complete),
            artifact_files=sum(r['artifact_files'] for r in complete))
        atomic_json(root/'manifest.json', report)
    return report
