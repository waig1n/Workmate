import { expect, it, vi } from 'vitest';
import { api } from './api';
import { resourceList } from './resourceList';
vi.mock('./api', () => ({ api: { get: vi.fn() } }));
it('collects subsequent pages and preserves selector filters', async () => {
  vi.mocked(api.get).mockResolvedValueOnce({ items: ['a'], total: 2 }).mockResolvedValueOnce({ items: ['b'], total: 2 });
  expect(await resourceList('/knowledge-bases?agent_scope_id=scope')).toEqual(['a', 'b']);
  expect(api.get).toHaveBeenLastCalledWith('/knowledge-bases?agent_scope_id=scope&page_size=100&page=2');
});
it('stops on an empty page when records disappear during pagination', async () => {
  vi.mocked(api.get).mockResolvedValueOnce({ items: [], total: 1 });
  expect(await resourceList('/agents')).toEqual([]);
});
