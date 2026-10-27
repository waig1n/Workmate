#!/usr/bin/env python3
"""Source-tree compatibility entry; installed CLI is scripts.compact_tool_transport."""
if __name__ == '__main__':
    import asyncio
    from scripts.compact_tool_transport import main
    asyncio.run(main())
