import argparse
import hashlib
import json
from pathlib import Path
from .store import connect
from .source import read_file,ingest
from . import facility,shift,complaint,report

HANDLERS={
 'facility-import':{'facility':facility.register,'defect':facility.defect,'repair':facility.repair},
 'shift-import':{'contract':shift.contract,'shift':shift.shift,'patrol':shift.patrol},
 'complaint-import':{'complaint':complaint.submit,'verify':complaint.verify,'remedy':complaint.remedy},
}

def import_file(db,command,path):
    raw,events=read_file(path)
    outputs=[]
    with db:
        sha=hashlib.sha256(raw).hexdigest()
        if db.execute('SELECT id FROM source_file WHERE command=? AND sha=?',(command,sha)).fetchone():
            return [{'result':'file_replayed'}]
        for event in events:
            kind=event['kind']
            if kind not in HANDLERS[command]:raise ValueError('event kind does not belong to subsystem')
            if ingest(db,event):outputs.append({'id':event['id'],'result':HANDLERS[command][kind](db,event)})
            else:outputs.append({'id':event['id'],'result':'event_replayed'})
        db.execute('INSERT INTO source_file(command,sha,raw) VALUES(?,?,?)',(command,sha,raw))
    return outputs

def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=[*HANDLERS,'demo','report','audit'])
    parser.add_argument('--db',default='service.sqlite')
    parser.add_argument('--file')
    args=parser.parse_args(argv)
    db=connect(args.db)
    if args.command=='demo':
        outcomes=[]
        for command,name in [('facility-import','facility-ok.json'),('shift-import','shift-ok.json'),('complaint-import','complaint-ok.json')]:
            outcomes.extend(import_file(db,command,Path(__file__).resolve().parents[1]/'fixtures'/name))
        result={'outcomes':outcomes,'report':report.report(db)}
    elif args.command=='report':result=report.report(db)
    elif args.command=='audit':result=report.audit(db,HANDLERS)
    else:
        if not args.file:parser.error('--file required')
        result={'outcomes':import_file(db,args.command,args.file),'report':report.report(db)}
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
