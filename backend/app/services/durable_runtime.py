"""Product role composition for durable execution and event projection."""
from workmate_product.projections.service import ProductProjectionService
from ..adapter.durable_interactions import interaction_projector
from functools import partial
from workmate_product.application.approval_expiry import expire_approval
from workmate_product.application.task_capacity import TaskCapacityPolicy


def compose_product_runtime(sessions,*,admit_next=None,poll_interval=.2,batch_size=100,capacity_policy=None,wakeup_bus=None):
    """Wire actual live/HITL/terminal/no-start projection plus immutable delivery.

    The role owns serve(shutdown) and must await it during shutdown. Current
    admission, snapshot freezing and RBAC are supplied as a trusted transaction
    service; no legacy RuntimeClient or orchestrator dispatch is called here.
    """
    return ProductProjectionService(sessions,project=interaction_projector(),
        admit_next=admit_next,poll_interval=poll_interval,batch_size=batch_size,wakeup_bus=wakeup_bus,
        expire_approval=partial(expire_approval,policy=capacity_policy) if capacity_policy else None)


def compose_product_services(sessions,settings,*,realtime_log,node_directory=None,wakeup_bus=None):
    """One Product execution chain; auxiliary App administration stays separate."""
    from .execution_admission import TaskAdmission
    from .product_runtime import ProductRuntime
    from .knowledge import knowledge_settings
    enabled=knowledge_settings().enabled
    admission=TaskAdmission(sessions,storage_node_id=settings.worker_storage_node_id,
        knowledge_enabled=enabled,node_directory=node_directory)
    policy=TaskCapacityPolicy.from_settings(settings)
    runtime=ProductRuntime(sessions,admission,knowledge_enabled=enabled,capacity_policy=policy)
    projection=compose_product_runtime(sessions,admit_next=admission.admit_next,capacity_policy=policy,wakeup_bus=wakeup_bus)
    from workmate_product.realtime.log_manager import EventLogManager
    from workmate_product.realtime.reader import ConversationReaderHub
    from .realtime_product_stream import RealtimeProductStream
    realtime = settings.realtime
    reader = ConversationReaderHub(realtime_log,
        page_events=realtime.reader_page_events,
        buffer_events=realtime.client_buffer_events,
        buffer_bytes=realtime.client_buffer_bytes,
        block_ms=realtime.reader_block_ms,
        max_readers=realtime.max_gateway_readers)
    stream = RealtimeProductStream(EventLogManager(sessions,realtime_log),
        realtime_log,reader,policy=realtime)
    return runtime,projection,stream
