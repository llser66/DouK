"""Separate local collection tools: no website login, no employee database access."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import server as app


TOOL_TABLES = {
    'tasks':'CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,payload TEXT NOT NULL)',
    'records':'CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY,task_id TEXT NOT NULL,payload TEXT NOT NULL)',
    'files':'CREATE TABLE IF NOT EXISTS files(key TEXT PRIMARY KEY,path TEXT NOT NULL)',
    'accounts':'CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,payload TEXT NOT NULL)',
}


def initialize_tools(source_db, source_config, target):
    target=Path(target)
    target.mkdir(parents=True,exist_ok=True)
    db_path=target/'workspace.sqlite'
    with closing(sqlite3.connect(db_path)) as dest, dest:
        for schema in TOOL_TABLES.values():
            dest.execute(schema)
        dest.execute('CREATE TABLE IF NOT EXISTS tools_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        if not dest.execute("SELECT 1 FROM tools_meta WHERE key='legacy_imported'").fetchone():
            with closing(sqlite3.connect(source_db)) as source, source:
                source.execute('BEGIN')
                for table in TOOL_TABLES:
                    if not source.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',('table',table)).fetchone():
                        continue
                    for row in source.execute(f'SELECT * FROM {table}'):
                        if table=='tasks':
                            task=json.loads(row[1])
                            if task.get('status') in {'queued','running','recording'}:
                                task.update(status='failed',message='任务已迁移到独立工具，请检查后重试')
                            row=(row[0],json.dumps(task,ensure_ascii=False))
                        dest.execute(f'INSERT OR IGNORE INTO {table} VALUES({",".join("?" for _ in row)})',row)
            dest.execute("INSERT INTO tools_meta VALUES('legacy_imported','1')")
    config_path=target/'settings.json'
    if not config_path.exists() and Path(source_config).exists():
        # Existing collection credentials only; employee credentials live in separate ops_* tables.
        config_path.write_bytes(Path(source_config).read_bytes())
    return db_path


class ToolHandler(app.Handler):
    app_mode='tools'


def configure(target=None):
    target=Path(target or app.ROOT/'data/tools')
    source_db,source_config=app.DB,app.CONFIG
    app.DB=initialize_tools(source_db,source_config,target)
    app.DATA=target
    app.CONFIG=target/'settings.json'
    # Existing media files remain available; the employee application does not write downloads.
    app.DOWNLOADS=app.ROOT/'Downloads'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8879)
    args=parser.parse_args()
    configure()
    app.migrate_legacy_account()
    for task in app.all_tasks():
        if task['status'] in {'queued','running','recording'}:
            app.save_task(task,status='failed',message='工具服务曾中断，可检查后重试；已下载部分保留')
    app.LOGS.write('INFO','system','独立采集工具服务启动')
    server=app.ThreadingHTTPServer(('127.0.0.1',args.port),ToolHandler)
    print(f'DouK Tools: http://127.0.0.1:{args.port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for session in app.LOGIN_SESSIONS.values():
            session.cancel()
        for event in app.CANCEL.values():
            event.set()
        for process in app.PROCESSES.values():
            if process.poll() is None:
                process.terminate()
        server.server_close()


if __name__=='__main__':
    main()
