#!/usr/bin/env python3
"""Source-tree compatibility entry; installed CLI is scripts.audit_tool_content_objects."""
from workmate_product.application.tool_content_gc import classify, report
if __name__ == '__main__':
    import asyncio
    from scripts.audit_tool_content_objects import main
    asyncio.run(main())
