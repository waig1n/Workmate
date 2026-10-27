import { expect, it } from 'vitest';
import fixture from './fixtures/reset-business-state.json';
import { parseBusinessState } from './realtime-business-state';
it('accepts unattended tasks in durable replay with schedule provenance',()=>{
 const tasks=fixture.tasks.map(task=>({...task,approval_profile:'unattended',origin:'schedule',schedule_occurrence_id:'occurrence'}));
 expect(parseBusinessState({...fixture,tasks},tasks[0].conversation_id)?.tasks[0]).toMatchObject({approval_profile:'unattended',origin:'schedule'});
});
it('preserves channel provenance during replay without changing approval profile',()=>{
 const tasks=fixture.tasks.map(task=>({...task,origin:'channel'}));
 expect(parseBusinessState({...fixture,tasks},tasks[0].conversation_id)?.tasks[0]).toMatchObject({origin:'channel',approval_profile:tasks[0].approval_profile});
});
