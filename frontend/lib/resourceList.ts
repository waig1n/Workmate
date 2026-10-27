import { api } from './api';
import type { ResourcePage } from './types';

/** Collect bounded pages for selectors that need the complete visible catalogue. */
export async function resourceList<T>(path: string): Promise<T[]> {
  const [pathname, query] = path.split('?');
  const params = new URLSearchParams(query);
  params.set('page_size', '100');
  const items: T[] = [];
  for (let page = 1; ; page += 1) {
    params.set('page', String(page));
    const result = await api.get<ResourcePage<T>>(`${pathname}?${params}`);
    items.push(...result.items);
    if (!result.items.length || items.length >= result.total) return items;
  }
}
