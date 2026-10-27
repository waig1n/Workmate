"""Versioned immutable delivery descriptors shared by Product and Worker."""
import hashlib
import json
import re

class DeliveryConflict(RuntimeError):
    pass


def _digest(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _path(value):
    return (isinstance(value,str) and 0<len(value)<=255 and not value.startswith('/') and '\\' not in value
        and all(p not in {'','.','..'} for p in value.split('/')))


def _validate(payload,*,capture):
    if not isinstance(payload,dict) or type(payload.get('version')) is not int or payload['version']!=1:
        raise DeliveryConflict('Unknown delivery metadata format')
    if capture:
        if (set(payload)!={'version','source','declared_files','warnings','files'} or payload['source'] not in {
                'snapshot','explicit','strict_no_declaration','snapshot_fallback'}
                or not isinstance(payload['files'],list) or len(payload['files'])>1000
                or not isinstance(payload['warnings'],list) or any(not isinstance(w,str) for w in payload['warnings'])
                or not isinstance(payload['declared_files'],list) or any(not _path(n) for n in payload['declared_files'])):
            raise DeliveryConflict('Malformed captured delivery manifest')
        seen=set()
        for file in payload['files']:
            if (not isinstance(file,dict) or set(file)!={'name','size','sha256','storage_key','change_type'}
                    or not _path(file['name']) or file['name'] in seen
                    or type(file['size']) is not int or file['size']<0
                    or not isinstance(file['sha256'],str) or re.fullmatch('[a-f0-9]{64}',file['sha256']) is None
                    or file['storage_key']!=f".objects/sha256/{file['sha256'][:2]}/{file['sha256']}"
                    or file['change_type'] not in {'created','modified','presented'}):
                raise DeliveryConflict('Invalid immutable artifact descriptor')
            seen.add(file['name'])
    elif (set(payload)!={'version','files','presentation_id'} or not isinstance(payload['files'],dict)
            or any(not _path(name) for name in payload['files'])
            or (payload['presentation_id'] is not None and not isinstance(payload['presentation_id'],str))):
        raise DeliveryConflict('Malformed delivery baseline')
    if len(json.dumps(payload).encode())>4*1024*1024:
        raise DeliveryConflict('Delivery metadata exceeds its bound')


