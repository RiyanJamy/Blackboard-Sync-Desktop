"""Unified manual queue for failed requests and size exclusions."""
from pathlib import Path
import re
from urllib.parse import quote


def key(course_id, item_id=None, attachment_id=None):
    return ':'.join((str(course_id), str(item_id or ''), str(attachment_id or '')))


def find_existing(record, config):
    if record.get('kind') != 'file' or not record.get('filename'):
        return []
    entry=config.courses.get(record.get('course_id'))
    if not entry or not entry.destination:
        return []
    root=Path(entry.destination)
    name=record['filename'].casefold()
    matches=[]
    if root.exists():
        for path in root.rglob('*'):
            if path.is_file() and path.name.casefold()==name and '_bbsync_backups' not in path.parts:
                matches.append(path)
    return matches


def enqueue(manifest, config, course_id, item_id=None, attachment_id=None,
            title='', filename='', path='', modified=None, reason='', kind='file', size=None):
    course=config.courses.get(course_id)
    record={'course_id':course_id,'item_id':item_id,'attachment_id':attachment_id,
            'course_name':course.name if course else course_id,'title':title,
            'filename':filename,'path':str(path),'modified':modified,
            'reason':re.sub(r'https?://\S+', '[request URL]', (str(reason).split('Call log:')[0].splitlines() or ['Request failed'])[0])[:500],
            'kind':kind,'size':size}
    queue=manifest._data.setdefault('manual',{})
    previous=queue.get(key(course_id,item_id,attachment_id))
    if previous and previous.get('modified')==modified:
        record['existing_at_report']=previous.get('existing_at_report',True)
    else:
        record['existing_at_report']=bool(find_existing(record,config))
    queue[key(course_id,item_id,attachment_id)]=record
    manifest.save()


def clear(manifest,course_id,item_id=None,attachment_id=None):
    manifest._data.setdefault('manual',{}).pop(key(course_id,item_id,attachment_id),None)


def entries(manifest,config):
    rows=dict(manifest._data.setdefault('manual',{}))
    # Import exclusions from older versions without requiring a new failed transfer.
    for aid,state in manifest._data['attachments'].items():
        if state.get('skipped') and state.get('course_id'):
            k=key(state['course_id'],state.get('item_id'),aid)
            rows.setdefault(k,{**state,'attachment_id':aid,'reason':state['skipped'],
                                'kind':'file','existing_at_report':True})
    return rows


def adopt_new_arrival(manifest,config,course_id,item_id,attachment_id,modified):
    record=entries(manifest,config).get(key(course_id,item_id,attachment_id))
    # Never assume a pre-existing or newly annotated original is a revised remote file.
    if not record or record.get('existing_at_report',True) or record.get('modified')!=modified:
        return False
    matches=find_existing(record,config)
    if len(matches)!=1:
        return False
    path=matches[0]
    manifest.record_attachment(attachment_id,str(path),modified)
    manifest._data['attachments'][attachment_id]['adopted_by_filename']=True
    clear(manifest,course_id,item_id,attachment_id)
    manifest.save()
    return True


def download_url(base_url,record):
    if record.get('attachment_id'):
        ids=[quote(str(record[k]),safe='') for k in ('course_id','item_id','attachment_id')]
        return base_url+'/learn/api/public/v1/courses/'+ids[0]+'/contents/'+ids[1]+'/attachments/'+ids[2]+'/download'
    return base_url+'/ultra/courses/'+quote(str(record['course_id']),safe='')+'/outline'
