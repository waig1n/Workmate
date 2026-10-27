"""Fail-closed checks for the bounded deployment load report (stdlib only)."""


def validate_report(report):
    errors = []
    if not isinstance(report, dict) or not isinstance(report.get('runs'), list):
        return ['missing report or runs']
    runs = report['runs']
    if 'window' in report and report['window'].get('passed') is not True:
        errors.append('incomplete or interrupted continuous window')
    if not runs or len(runs) != report.get('expected_runs'):
        errors.append('incomplete repetitions')
    for index, run in enumerate(runs):
        prefix = f'run {index}: '
        if not run.get('correct') or not run.get('budget_pass'):
            errors.append(prefix + 'correctness or latency budget failed')
        tasks = run.get('tasks', [])
        ids = [task.get('id') for task in tasks]
        if (not tasks or len(tasks) != run.get('expected_tasks') or
                len(set(ids)) != len(ids) or any(not value for value in ids)):
            errors.append(prefix + 'missing or duplicate tasks')
        if any(task.get('terminal') is not True or task.get('effect_count') != 1 for task in tasks):
            errors.append(prefix + 'missing terminal or duplicate/missing completion effect')
        file_tasks=[t for t in tasks if t.get('workload',report.get('workload')) in ('file','approval','skill','mcp')]
        if any(
                not isinstance(task.get('file_evidence'), dict) or
                task['file_evidence'].get('write_attempt_count') != 1 or
                not task['file_evidence'].get('download_sha256') or
                not task['file_evidence'].get('download_bytes') for task in file_tasks):
            errors.append(prefix + 'missing or duplicate file effect/download evidence')
        for task in tasks:
            case=task.get('workload',report.get('workload'))
            if case=='rag' and not task.get('rag_evidence'):
                errors.append(prefix+'missing knowledge source evidence')
            if case in ('approval','mcp') and not task.get('approval_evidence'):
                errors.append(prefix+'missing approval evidence')
        if run.get('unfinished_commands') != []:
            errors.append(prefix + 'unfinished commands')
        for key, expected in (('command_outbox', 'settled'), ('business_outbox', 'published')):
            rows = run.get(key)
            if not isinstance(rows, list) or not rows or any(row.get('status') != expected for row in rows):
                errors.append(prefix + key + ' not drained')
        groups = run.get('pel')
        if not isinstance(groups, list) or not groups or any(
                group.get('group', {}).get('pending') != 0 or
                group.get('group', {}).get('lag') != 0 for group in groups):
            errors.append(prefix + 'consumer backlog not drained')
    return errors
