"""Permission gates accept only implemented, evidenced containment combinations."""
import pytest
from workmate_product.application.sandbox_proof import sandbox_proof_valid


@pytest.mark.parametrize('provider',['docker','kubernetes','e2b','daytona','opensandbox'])
def test_remote_proof_requires_instance_and_evidence(provider):
    proof={'isolation':provider,'containment':'dedicated_uid','instance_id':'actual-instance',
           'evidence':{'uid':60000,'no_new_privs':True}}
    assert sandbox_proof_valid(proof)
    for patch in ({'instance_id':''},{'evidence':{}},{'containment':'pid_namespace'},
                  {'evidence':{'uid':0,'no_new_privs':True}}, {'evidence':{'uid':60000,'no_new_privs':False}}):
        assert not sandbox_proof_valid({**proof,**patch})


def test_existing_bubblewrap_proof_and_unknown_backend():
    assert sandbox_proof_valid({'isolation':'bubblewrap','containment':'pid_namespace'})
    assert not sandbox_proof_valid({'isolation':'local','containment':'dedicated_uid',
        'instance_id':'host','evidence':{'uid':60000,'no_new_privs':True}})
    assert not sandbox_proof_valid(None)
