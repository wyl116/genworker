# edition: baseline
"""
Unit tests for task management tools (task_create, task_get, task_list, task_update).
"""
import pytest

from src.tools.builtin.task_store import Task, TaskStatus, TaskStore
from src.tools.builtin.task_tools import create_task_tools


@pytest.fixture
def store():
    return TaskStore()


@pytest.fixture
def tools(store):
    tool_tuple = create_task_tools(store)
    return {t.name: t for t in tool_tuple}


# ---- TaskStore unit tests ----

@pytest.mark.asyncio
async def test_store_create(store):
    task = await store.create(subject="Test task", description="Do something")
    assert task.id == "1"
    assert task.subject == "Test task"
    assert task.status == TaskStatus.PENDING


@pytest.mark.asyncio
async def test_store_sequential_ids(store):
    t1 = await store.create(subject="First", description="")
    t2 = await store.create(subject="Second", description="")
    assert t1.id == "1"
    assert t2.id == "2"


@pytest.mark.asyncio
async def test_store_get(store):
    created = await store.create(subject="Task", description="Desc")
    fetched = await store.get(created.id)
    assert fetched is not None
    assert fetched.subject == "Task"


@pytest.mark.asyncio
async def test_store_get_not_found(store):
    result = await store.get("999")
    assert result is None


@pytest.mark.asyncio
async def test_store_list_all(store):
    await store.create(subject="A", description="")
    await store.create(subject="B", description="")
    tasks = await store.list_all()
    assert len(tasks) == 2


@pytest.mark.asyncio
async def test_store_update_status(store):
    task = await store.create(subject="Task", description="")
    updated = await store.update(task.id, status=TaskStatus.IN_PROGRESS)
    assert updated is not None
    assert updated.status == TaskStatus.IN_PROGRESS
    # Original task is immutable
    original = await store.get(task.id)
    assert original.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_store_update_not_found(store):
    result = await store.update("999", status=TaskStatus.COMPLETED)
    assert result is None


@pytest.mark.asyncio
async def test_store_cascade_unblock(store):
    t1 = await store.create(subject="Blocker", description="")
    t2 = await store.create(subject="Blocked", description="")
    await store.update(t2.id, add_blocked_by=[t1.id])

    # Verify blocked
    t2_check = await store.get(t2.id)
    assert t1.id in t2_check.blocked_by

    # Complete blocker -> should unblock
    await store.update(t1.id, status=TaskStatus.COMPLETED)
    t2_after = await store.get(t2.id)
    assert t1.id not in t2_after.blocked_by


@pytest.mark.asyncio
async def test_store_delete(store):
    task = await store.create(subject="To delete", description="")
    deleted = await store.delete(task.id)
    assert deleted is True
    assert await store.get(task.id) is None


@pytest.mark.asyncio
async def test_store_delete_cascade_references(store):
    t1 = await store.create(subject="A", description="")
    t2 = await store.create(subject="B", description="")
    await store.update(t2.id, add_blocked_by=[t1.id])
    await store.update(t1.id, add_blocks=[t2.id])

    # Delete t1, should remove from t2's blocked_by
    await store.delete(t1.id)
    t2_after = await store.get(t2.id)
    assert t1.id not in t2_after.blocked_by


@pytest.mark.asyncio
async def test_store_delete_not_found(store):
    result = await store.delete("999")
    assert result is False


@pytest.mark.asyncio
async def test_store_metadata_merge(store):
    task = await store.create(subject="Task", description="", metadata={"key1": "val1"})
    updated = await store.update(task.id, metadata_merge={"key2": "val2"})
    assert updated.metadata["key1"] == "val1"
    assert updated.metadata["key2"] == "val2"


@pytest.mark.asyncio
async def test_store_metadata_delete_key(store):
    task = await store.create(subject="Task", description="", metadata={"a": 1, "b": 2})
    updated = await store.update(task.id, metadata_merge={"a": None})
    assert "a" not in updated.metadata
    assert updated.metadata["b"] == 2


# ---- Tool handler tests ----

@pytest.mark.asyncio
async def test_tool_create(tools):
    result = await tools["task_create"].handler(subject="My task", description="Details")
    assert "Created task #1" in result
    assert "My task" in result


@pytest.mark.asyncio
async def test_tool_create_empty_subject(tools):
    result = await tools["task_create"].handler(subject="   ", description="")
    assert "Error" in result


@pytest.mark.asyncio
async def test_tool_get(tools, store):
    await store.create(subject="Existing", description="Desc")
    result = await tools["task_get"].handler(task_id="1")
    assert "Existing" in result
    assert "Desc" in result


@pytest.mark.asyncio
async def test_tool_get_not_found(tools):
    result = await tools["task_get"].handler(task_id="999")
    assert "not found" in result


@pytest.mark.asyncio
async def test_tool_list_empty(tools):
    result = await tools["task_list"].handler()
    assert "No tasks" in result


@pytest.mark.asyncio
async def test_tool_list_with_tasks(tools, store):
    await store.create(subject="Task A", description="")
    t2 = await store.create(subject="Task B", description="")
    await store.update(t2.id, status=TaskStatus.IN_PROGRESS)

    result = await tools["task_list"].handler()
    assert "Task A" in result
    assert "Task B" in result
    assert "In Progress" in result
    assert "Pending" in result


@pytest.mark.asyncio
async def test_tool_update_status(tools, store):
    await store.create(subject="Task", description="")
    result = await tools["task_update"].handler(task_id="1", status="in_progress")
    assert "Updated" in result
    assert "[>]" in result


@pytest.mark.asyncio
async def test_tool_update_invalid_status(tools, store):
    await store.create(subject="Task", description="")
    result = await tools["task_update"].handler(task_id="1", status="invalid")
    assert "Error" in result
    assert "Invalid status" in result


@pytest.mark.asyncio
async def test_tool_update_delete(tools, store):
    await store.create(subject="Task", description="")
    result = await tools["task_update"].handler(task_id="1", status="deleted")
    assert "Deleted" in result
    assert await store.get("1") is None


@pytest.mark.asyncio
async def test_tool_update_dependencies(tools, store):
    await store.create(subject="A", description="")
    await store.create(subject="B", description="")
    result = await tools["task_update"].handler(task_id="2", add_blocked_by="1")
    assert "Updated" in result

    task_b = await store.get("2")
    assert "1" in task_b.blocked_by


@pytest.mark.asyncio
async def test_task_immutability(store):
    """Verify Task is frozen (immutable)."""
    task = await store.create(subject="Immutable", description="")
    with pytest.raises(AttributeError):
        task.subject = "Modified"


@pytest.mark.asyncio
async def test_tool_schema(tools):
    """Verify all tools have proper OpenAI schema."""
    for name, tool in tools.items():
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == name
        assert "parameters" in schema["function"]
        assert schema["function"]["parameters"]["type"] == "object"
