# -*- coding: utf-8 -*-
"""Full backup of one database, run as its own process (outside Odoo, so Odoo's time limits can't stop it).

    python3 backup_worker.py <job.json>

The job file (written by models/agent_backup.py, readable only by Odoo's user) is deleted as soon as it is
read: it holds the database password. The result is Odoo's standard backup zip, the same as the database
manager's: dump.sql, filestore/ and manifest.json. Progress is in the status file:
{"state": "running" | "done" | "failed", "size", "sha256", "error", "started", "finished"}.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile


def write_status(path, **values):
    temp = path + '.tmp'
    with open(temp, 'w') as f:
        json.dump(values, f)
    os.replace(temp, path)


def main(job_path):
    with open(job_path) as f:
        job = json.load(f)
    os.remove(job_path)
    status, target = job['status'], job['zip']
    started = time.time()
    write_status(status, state='running', started=started, pid=os.getpid())
    dump = target + '.sql'
    part = target + '.part'
    try:
        env = dict(os.environ, **job['pg_env'])
        result = subprocess.run([job['pg_dump'], '--no-owner', '--file=' + dump, job['db']], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            raise RuntimeError('pg_dump failed: ' + result.stderr.decode(errors='replace')[-400:])
        with zipfile.ZipFile(part, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.write(dump, 'dump.sql')
            archive.writestr('manifest.json', json.dumps(job['manifest'], indent=4))
            filestore = job['filestore']
            if os.path.isdir(filestore):
                for root, _dirs, files in os.walk(filestore):
                    for name in files:
                        full = os.path.join(root, name)
                        archive.write(full, os.path.join('filestore', os.path.relpath(full, filestore)))
        os.replace(part, target)
        digest = hashlib.sha256()
        with open(target, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                digest.update(chunk)
        write_status(status, state='done', started=started, finished=time.time(), size=os.path.getsize(target),
                     sha256=digest.hexdigest())
    except Exception as e:
        write_status(status, state='failed', started=started, finished=time.time(), error=str(e)[:500])
    finally:
        for leftover in (dump, part):
            if os.path.exists(leftover):
                os.remove(leftover)


if __name__ == '__main__':
    main(sys.argv[1])
