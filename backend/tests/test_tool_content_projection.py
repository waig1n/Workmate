"""Object references survive Runtime transport retention in public reset audit."""
import hashlib
from sqlalchemy import select
from test_durable_projection import projection_database, emit, consume  # noqa: F401
from workmate_product.persistence.models import TaskEvent
from workmate_product.persistence.repositories import tool_content, conversation_display
from app.services.conversation_business_state import _ACTIVITY_KEYS


async def test_reference_is_durable_public_audit_and_retrievable_after_transport_purge(projection_database):
    sessions,admin=projection_database
    digest=hashlib.sha256(b'full private content').hexdigest()
    key=f'.tool-content/sha256/{digest[:2]}/{digest}'
    ref={'kind':'metadata','sha256':digest,'size_bytes':20,'storage_key':key,
         'preview':'full','incomplete':False}
    events=[{'type':'REPLY_START','session_id':'s'},
            {'type':'TOOL_CALL_START','tool_call_id':'call','tool_call_name':'Write'},
            {'type':'TOOL_CONTENT_REFERENCE','tool_call_id':'call','content_ref':ref}]
    for seq,event in enumerate(events,1):
        await emit(admin,seq,event)
        await consume(sessions)
    async with sessions() as db:
        audits=await conversation_display.display_events(db,'c')
        content=next(event for event in audits if event.event_type=='tool_content')
        assert set(content.payload)==set(_ACTIVITY_KEYS['tool_content'])
        assert 'storage_key' not in content.payload
        assert content.payload['kind']=='metadata'
        assert key in await tool_content.referenced_keys(db)
    # Simulate removal of transport records in their FK dependency order. This
    # does not invoke a production PG retention job; durable Task audit remains.
    async with admin.transaction():
        await admin.execute("""DELETE FROM realtime_business_outbox WHERE event_id IN
            (SELECT id FROM product_events WHERE source_runtime_event_id IN
                (SELECT id FROM runtime_events WHERE payload->>'type'='TOOL_CONTENT_REFERENCE'))""")
        await admin.execute("""DELETE FROM product_events WHERE source_runtime_event_id IN
            (SELECT id FROM runtime_events WHERE payload->>'type'='TOOL_CONTENT_REFERENCE')""")
        await admin.execute("DELETE FROM runtime_events WHERE payload->>'type'='TOOL_CONTENT_REFERENCE'")
    async with sessions() as db:
        assert (await tool_content.find_content(db,'t','call','metadata'))['storage_key']==key
        assert await tool_content.find_content(db,'other','call','metadata') is None
        assert key in await tool_content.referenced_keys(db)
