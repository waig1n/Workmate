import asyncio
import argparse
from pathlib import Path
import pytest
from scripts.reliability import capacity_campaign as c


def test_matrix_classification_rejects_missing_tasks():
    assert c.classification({'sample_validity':{'valid':True},'runs':[]})=='incomplete'


def test_sender_timeout_awaits_supervisor_cancellation_cleanup(monkeypatch,tmp_path):
    finished=[]
    async def supervise(args,client_args):
        try:await asyncio.sleep(99)
        finally:
            await asyncio.sleep(.01)
            finished.append('reaped')
    monkeypatch.setattr(c,'supervise_sender',supervise)
    args=argparse.Namespace(base_url='http://127.0.0.1:8080',dsn='postgresql://127.0.0.1/fixture',redis_url='redis://127.0.0.1',monitor=tmp_path)
    with pytest.raises(TimeoutError):asyncio.run(c.run_sender(args,'capacity-0123456789ab',tmp_path/'run',1,timeout=.01))
    assert finished==['reaped']


def test_campaign_pass_requires_all_supervisor_and_metrics_receipts():
    levels=[{'classification':'passed','sender_exit':0,'metrics_passed':True} for _ in range(4)]
    assert c.campaign_passed(levels,{'passed':True})
    levels[2]['sender_exit']=1
    assert not c.campaign_passed(levels,{'passed':True})


def test_campaign_sender_error_still_stops_monitor_and_cleans_owned_sut(tmp_path,monkeypatch):
    import json
    owner='capacity-0123456789ab';state=tmp_path/'state.json';state.write_text(json.dumps({'owner':owner}))
    monitor=tmp_path/'monitor';monitor.mkdir();cleaned=[]
    class Remote:
        def __init__(self,s,p):assert s['owner']==owner
        def cleanup(self):cleaned.append(owner);return {'passed':True}
    async def sender(*args,**kwargs):raise TimeoutError('bounded sender')
    monkeypatch.setattr(c,'Remote',Remote);monkeypatch.setattr(c,'run_sender',sender)
    args=argparse.Namespace(state=state,monitor=monitor,output=tmp_path/'campaign',base_url='http://127.0.0.1',dsn='postgresql://127.0.0.1/fixture',redis_url='redis://127.0.0.1')
    result=asyncio.run(c.campaign(args))
    assert not result['passed'] and result['error']['type']=='TimeoutError'
    assert cleaned==[owner] and (monitor/'monitor.stop').exists()
    assert json.loads((args.output/'campaign.json').read_text())['cleanup']['passed']


@pytest.fixture(autouse=True)
def explicit_fixture_remote(monkeypatch):
    monkeypatch.setenv('WORKMATE_TEST_SSH_TARGET', 'operator@example.test')
