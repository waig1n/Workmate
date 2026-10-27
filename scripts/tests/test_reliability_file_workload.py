import json
import pytest
from scripts.reliability.load_support.file_workload import file_content,verify_file


def test_content_size():
    assert len(file_content('a'*32,1024).encode())==1024


@pytest.mark.parametrize('key,size',[('../escape',1024),('a'*32,0),('a'*32,2**21)])
def test_invalid_fixture(key,size):
    with pytest.raises(ValueError):file_content(key,size)


def test_duplicate_write_is_detected(tmp_path):
    key='a'*32;payload=file_content(key,1024).encode()
    row={'file_path':'/workspace/output/'+key+'.txt'}
    trace=tmp_path/'write-effects-a.jsonl'
    trace.write_text(json.dumps(row)+'\n')
    assert verify_file(key,payload,1024,tmp_path)['write_attempt_count']==1
    trace.write_text((json.dumps(row)+'\n')*2)
    with pytest.raises(AssertionError):verify_file(key,payload,1024,tmp_path)


def test_bad_download_and_missing_effect_are_rejected(tmp_path):
    key='b'*32
    with pytest.raises(AssertionError):verify_file(key,b'wrong',1024,tmp_path)
    with pytest.raises(AssertionError):verify_file(key,file_content(key,1024).encode(),1024,tmp_path)


def test_file_option_is_explicit():
    from scripts.reliability.load import parse_args,plan
    value=plan(parse_args(['--gateway-image','g','--runtime-image','r','--workload','file']))
    assert value['workload']=='file' and value['file_bytes']==1024
