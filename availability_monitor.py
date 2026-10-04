"""Small external availability monitor. No service restarts, migrations or secret logs."""
from __future__ import annotations
import argparse
import fcntl
import importlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def save(path, state):
    path=Path(path)
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.is_symlink() or path.parent.absolute()!=path.parent.resolve():raise RuntimeError('Unsafe monitor state path')
    tmp=path.with_name(path.name+'.new')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as out:
        json.dump(state,out);out.flush();os.fsync(out.fileno())
    os.replace(tmp,path)


def probe(url):
    # Monitoring requires only public API status; discard arbitrary response body.
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme not in {'http','https'} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Monitor URL must be an uncredentialled public base URL.')
    try:
        req=urllib.request.Request(url.rstrip('/')+'/api/server/ping',headers={'User-Agent':'Immich-availability-monitor/1'})
        with urllib.request.urlopen(req,timeout=10) as response:
            raw=response.read(4097)
            if response.status!=200 or len(raw)>4096:return {'healthy':False,'reason':'unexpected_response'}
            data=json.loads(raw)
        return {'healthy':isinstance(data,dict) and data.get('res')=='pong','reason':'ping'}
    except urllib.error.HTTPError as exc:
        return {'healthy':False,'reason':'http_'+str(exc.code)}
    except (urllib.error.URLError,TimeoutError,OSError,json.JSONDecodeError,ValueError):
        return {'healthy':False,'reason':'unreachable_or_invalid_response'}


def check(state, observation, *, url, sender, persist, now=None, failure_threshold=3):
    now=time.time() if now is None else now
    state['last_check']=now
    if observation.get('healthy') is True:
        # Re-arm silently; do not deliver a queued obsolete outage after recovery.
        state.update(failures=0,notified=False)
        state.pop('pending',None);state.pop('first_failure',None)
        persist(state)
        return None
    state.setdefault('first_failure',now)
    state['failures']=state.get('failures',0)+1
    state['last_reason']=observation['reason']
    if state['failures']<failure_threshold or state.get('notified'):
        persist(state);return None
    if 'pending' not in state:
        state['pending']={'text':('⚠️ Хозяин, Immich недоступен при нескольких последовательных внешних проверках.\n'
             'Адрес: '+url+'\n'
             'Причина пока не установлена: это может быть обновление, сервер или сетевой путь.\n'
             'Нужно зайти на VM с Immich и проверить сервис и журнал обновлятора.\n'
             'Проверка ничего не перезапускает и не изменяет.'),'attempts':0,'next_attempt':0}
    pending=state['pending']
    if now<pending['next_attempt']:
        persist(state);return None
    pending['attempts']+=1;pending['next_attempt']=now+600
    persist(state) # Durable pending event precedes the external send.
    try:
        receipt=sender(pending['text'])
        if not isinstance(receipt,dict) or not receipt.get('message_id'):raise RuntimeError('No delivery receipt')
    except Exception as exc:
        pending['error_type']=type(exc).__name__;persist(state);return None
    state['receipt']=receipt;state['notified']=True;state.pop('pending');persist(state)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--state',required=True)
    parser.add_argument('--sender-directory',required=True)
    parser.add_argument('--chat-id',required=True,type=int)
    parser.add_argument('--topic-id',required=True,type=int)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    sys.path.insert(0,args.sender_directory)
    send=importlib.import_module('notification_outbox').send
    path=Path(args.state);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.is_symlink() or path.parent.absolute()!=path.parent.resolve():raise RuntimeError('Unsafe state path')
    fd=os.open(path.with_suffix('.lock'),os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        state=json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(state,dict):raise RuntimeError('Invalid monitor state')
        while True:
            receipt=check(state,probe(args.url),url=args.url,
                          sender=lambda text:send(text,chat_id=args.chat_id,topic_id=args.topic_id),
                          persist=lambda value:save(path,value))
            if receipt:print(json.dumps({'event':'outage_notice_sent',**receipt}),flush=True)
            if args.once:return 0
            time.sleep(60)
    finally:os.close(fd)


if __name__=='__main__':raise SystemExit(main())
