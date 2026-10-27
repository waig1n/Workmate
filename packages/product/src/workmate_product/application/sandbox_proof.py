"""Shared fail-closed permission gate for immutable provider attestations."""
import json

REMOTE_PROVIDERS = frozenset({'docker','kubernetes','e2b','daytona','opensandbox'})


def sandbox_proof_valid(proof):
    if not proof:
        return False
    if proof['isolation']=='bubblewrap':
        return proof['containment']=='pid_namespace'
    if proof['isolation'] not in REMOTE_PROVIDERS or proof['containment']!='dedicated_uid':
        return False
    if not str(proof.get('instance_id') or '').strip():
        return False
    evidence=proof.get('evidence')
    if isinstance(evidence,str):
        try:
            evidence=json.loads(evidence)
        except ValueError:
            return False
    return (isinstance(evidence,dict) and type(evidence.get('uid')) is int
        and evidence['uid']>0 and evidence.get('no_new_privs') is True)


def sandbox_proof_sql(alias='p'):
    """PostgreSQL counterpart; alias is an internal literal, never user input."""
    return f"""(({alias}.isolation='bubblewrap' AND {alias}.containment='pid_namespace') OR
        ({alias}.isolation IN ('docker','kubernetes','e2b','daytona','opensandbox')
        AND {alias}.containment='dedicated_uid' AND length(trim({alias}.instance_id))>0
        AND json_typeof({alias}.evidence->'uid')='number'
        AND ({alias}.evidence->>'uid') ~ '^[1-9][0-9]*$'
        AND json_typeof({alias}.evidence->'no_new_privs')='boolean'
        AND {alias}.evidence->>'no_new_privs'='true'))"""
