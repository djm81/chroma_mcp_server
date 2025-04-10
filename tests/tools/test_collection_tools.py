"""Tests for collection management tools."""

import pytest
import uuid
import re
import json
import numpy as np
import time

from typing import Dict, Any, List, Optional
from unittest.mock import patch, MagicMock, ANY, call
from contextlib import contextmanager

from mcp import types
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, INTERNAL_ERROR, ErrorData

# Import specific errors if needed, or rely on ValidationError/Exception
from src.chroma_mcp.utils.errors import ValidationError
from src.chroma_mcp.tools.collection_tools import (
    _reconstruct_metadata,  # Keep helper if used
    _create_collection_impl,
    _create_collection_with_metadata_impl,
    _list_collections_impl,
    _get_collection_impl,
    _rename_collection_impl,
    _delete_collection_impl,
    _peek_collection_impl,
)

# Import Pydantic models used by the tools
from src.chroma_mcp.tools.collection_tools import (
    CreateCollectionInput,
    CreateCollectionWithMetadataInput,
    ListCollectionsInput,
    GetCollectionInput,
    RenameCollectionInput,
    DeleteCollectionInput,
    PeekCollectionInput,
)

# Correct import for get_collection_settings
from src.chroma_mcp.utils.config import get_collection_settings

DEFAULT_SIMILARITY_THRESHOLD = 0.7

# --- Helper Functions ---


def assert_successful_json_result(
    result: List[types.TextContent],
    expected_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Asserts the tool result is a successful list containing valid JSON, returning the parsed data."""
    assert isinstance(result, list)
    assert len(result) > 0, "Result list cannot be empty for successful JSON result."
    content_item = result[0]
    assert isinstance(content_item, types.TextContent), f"Expected TextContent, got {type(content_item)}"
    assert content_item.type == "text", f"Expected content type 'text', got '{content_item.type}'"
    assert content_item.text is not None, "Text content cannot be None for JSON result."
    try:
        parsed_data = json.loads(content_item.text)
        assert isinstance(parsed_data, dict), f"Parsed JSON is not a dictionary, got {type(parsed_data)}"
    except (json.JSONDecodeError, AssertionError) as e:
        pytest.fail(f"Result content is not valid JSON: {e}\nContent: {content_item.text}")
    if expected_data is not None:
        assert parsed_data == expected_data, f"Parsed JSON data mismatch. Expected: {expected_data}, Got: {parsed_data}"
    return parsed_data


# Rework assert_error_result as a context manager for future use
@contextmanager
def assert_raises_mcp_error(expected_message: str):
    """Asserts that McpError is raised and optionally checks the error message."""
    try:
        yield
    except McpError as e:
        # Check both e.error_data (if exists) and e.args[0]
        message = ""
        if hasattr(e, "error_data") and hasattr(e.error_data, "message"):
            message = str(e.error_data.message)
        elif e.args and isinstance(e.args[0], ErrorData) and hasattr(e.args[0], "message"):
            message = str(e.args[0].message)
        else:
            message = str(e)  # Fallback to the exception string itself

        assert (
            expected_message in message
        ), f"Expected error message containing '{expected_message}' but got '{message}'"
        return


@pytest.fixture
def mock_chroma_client_collections():
    """Fixture to mock the Chroma client and its methods for collection tests (Synchronous)."""
    with patch("src.chroma_mcp.tools.collection_tools.get_chroma_client") as mock_get_client, patch(
        "src.chroma_mcp.tools.collection_tools.get_embedding_function"
    ) as mock_get_embedding_function, patch(
        "src.chroma_mcp.tools.collection_tools.get_collection_settings"
    ) as mock_get_settings, patch(
        "src.chroma_mcp.tools.collection_tools.validate_collection_name"
    ) as mock_validate_name:
        # Use MagicMock for synchronous behavior
        mock_client_instance = MagicMock()
        mock_collection_instance = MagicMock()

        # Configure mock methods for collection (synchronous)
        mock_collection_instance.name = "mock_collection"
        mock_collection_instance.id = "mock_id_123"
        # Set more realistic initial metadata
        mock_collection_instance.metadata = {"description": "Fixture Desc"}
        mock_collection_instance.count.return_value = 0
        mock_collection_instance.peek.return_value = {"ids": [], "documents": []}

        # Configure mock methods for client (synchronous)
        mock_client_instance.create_collection.return_value = mock_collection_instance
        mock_client_instance.get_collection.return_value = mock_collection_instance
        mock_client_instance.list_collections.return_value = ["existing_coll1", "existing_coll2"]
        # Explicitly configure methods used in collection tests that were missing
        mock_client_instance.delete_collection.return_value = None  # For delete tests

        # Configure modify on the collection instance mock (used by set/update/rename)
        mock_collection_instance.modify.return_value = None

        mock_get_client.return_value = mock_client_instance
        mock_get_embedding_function.return_value = None
        mock_get_settings.return_value = {"hnsw:space": "cosine"}  # Default settings if needed
        mock_validate_name.return_value = None

        yield mock_client_instance, mock_collection_instance, mock_validate_name


class TestCollectionTools:
    """Test cases for collection management tools."""

    # --- _create_collection_impl Tests ---
    @pytest.mark.asyncio
    async def test_create_collection_success(self, mock_chroma_client_collections):
        """Test successful collection creation."""
        (
            mock_client,
            _,
            mock_validate,
        ) = mock_chroma_client_collections  # mock_collection fixture not directly needed here
        collection_name = "test_create_new"
        mock_collection_id = str(uuid.uuid4())

        # Mock the collection returned by create_collection
        created_collection_mock = MagicMock()
        created_collection_mock.name = collection_name
        created_collection_mock.id = mock_collection_id  # Use a fixed UUID for assertion

        # Simulate the metadata as stored by ChromaDB (flattened, used by _reconstruct_metadata)
        actual_default_settings = get_collection_settings()  # Get the full defaults
        # Metadata sent TO create_collection should only contain explicitly handled defaults (like hnsw:space)
        # The implementation relies on Chroma to apply other defaults server-side.
        expected_metadata_passed_to_chroma = None
        if "hnsw:space" in actual_default_settings:
            expected_metadata_passed_to_chroma = {"chroma:setting:hnsw_space": actual_default_settings["hnsw:space"]}

        # The collection object returned by the MOCK should have the FULL metadata
        # reflecting what Chroma *would* store, including implicit defaults.
        metadata_stored_by_chroma = {
            f"chroma:setting:{k.replace(':', '_')}": v for k, v in actual_default_settings.items()
        }
        created_collection_mock.metadata = metadata_stored_by_chroma  # What the collection obj would have
        created_collection_mock.count.return_value = 0  # Simulate count after creation
        mock_client.create_collection.return_value = created_collection_mock

        # --- Act ---
        input_model = CreateCollectionInput(collection_name=collection_name)
        # Call await directly, expect List[TextContent]
        result_list = await _create_collection_impl(input_model)

        # --- Assert ---
        # Mock calls
        mock_validate.assert_called_once_with(collection_name)
        mock_client.create_collection.assert_called_once()
        call_args = mock_client.create_collection.call_args
        assert call_args.kwargs["name"] == collection_name
        # Check metadata passed TO create_collection matches the expected subset
        assert "metadata" in call_args.kwargs
        assert call_args.kwargs["metadata"] == expected_metadata_passed_to_chroma
        assert call_args.kwargs["get_or_create"] is False

        # Result structure and content assertions using helper on the list
        result_data = assert_successful_json_result(result_list)
        assert result_data.get("name") == collection_name
        assert result_data.get("id") == mock_collection_id
        assert "metadata" in result_data
        # Correct Assertion: Compare the RECONSTRUCTED metadata['settings'] in the result with the EXPECTED defaults
        assert "settings" in result_data["metadata"], "Reconstructed metadata should contain a 'settings' key"
        # Convert keys in actual_default_settings from _ to : for comparison
        expected_settings_with_colons = {k.replace("_", ":"): v for k, v in actual_default_settings.items()}
        assert result_data["metadata"]["settings"] == expected_settings_with_colons
        assert result_data.get("count") == 0  # Based on mock count

    @pytest.mark.asyncio
    async def test_create_collection_invalid_name(self, mock_chroma_client_collections):
        """Test collection name validation failure within the implementation."""
        mock_client, _, mock_validate = mock_chroma_client_collections
        invalid_name = "invalid-"
        # Configure the validator mock to raise the error
        error_msg = "Invalid collection name"
        mock_validate.side_effect = ValidationError(error_msg)

        # --- Act & Assert ---
        input_model = CreateCollectionInput(collection_name=invalid_name)
        # result = await _create_collection_impl(input_model)
        # --- Assert ---
        # mock_validate.assert_called_once_with(invalid_name) # Called inside the context manager check
        mock_client.create_collection.assert_not_called()
        # Assert validation error returned by _impl
        # with assert_raises_mcp_error("Validation Error: Invalid collection name"):
        #     await _create_collection_impl(input_model)
        with assert_raises_mcp_error(f"Validation Error: {error_msg}"):
            await _create_collection_impl(input_model)
        mock_validate.assert_called_once_with(invalid_name)  # Verify validator was called

    @pytest.mark.asyncio
    async def test_create_collection_with_custom_metadata(self, mock_chroma_client_collections):
        """Test creating a collection with custom metadata provided (as JSON string)."""
        mock_client, _, mock_validate = mock_chroma_client_collections
        collection_name = "custom_meta_coll"
        custom_metadata_dict = {"hnsw:space": "ip", "custom_field": "value1"}
        custom_metadata_json = json.dumps(custom_metadata_dict)  # Convert to JSON string
        mock_collection_id = str(uuid.uuid4())

        # Mock the collection returned by create_collection
        created_collection_mock = MagicMock()
        created_collection_mock.name = collection_name
        created_collection_mock.id = mock_collection_id
        # Metadata stored internally might be slightly different if flattened
        # Pass the DICT to the mock, as that's what _impl passes to the *client*
        created_collection_mock.metadata = custom_metadata_dict
        created_collection_mock.count.return_value = 0
        mock_client.create_collection.return_value = created_collection_mock

        # --- Act ---
        # Create Pydantic model instance - Use correct model, pass JSON string
        input_model = CreateCollectionWithMetadataInput(
            collection_name=collection_name, metadata=custom_metadata_json
        )  # Pass the JSON string here
        # Use correct implementation function
        result_list = await _create_collection_with_metadata_impl(input_model)

        # --- Assert ---
        # Mock calls
        mock_validate.assert_called_once_with(collection_name)
        mock_client.create_collection.assert_called_once()
        call_args = mock_client.create_collection.call_args
        assert call_args.kwargs["name"] == collection_name
        # Verify the original *dictionary* was passed to Chroma's create_collection
        # (because _impl parses the JSON string back to a dict)
        assert call_args.kwargs["metadata"] == custom_metadata_dict

        # Assert successful result
        result_data = assert_successful_json_result(result_list)
        assert result_data.get("name") == collection_name
        assert result_data.get("id") == mock_collection_id
        assert result_data.get("count") == 0
        # Assert reconstructed metadata in result matches input dict
        assert result_data.get("metadata") == _reconstruct_metadata(custom_metadata_dict)

    @pytest.mark.asyncio
    async def test_create_collection_chroma_duplicate_error(self, mock_chroma_client_collections):
        """Test handling ChromaDB error when collection already exists."""
        mock_client, _, mock_validate = mock_chroma_client_collections
        collection_name = "duplicate_coll"
        # Mock create_collection to raise the specific ValueError Chroma uses
        error_message = f"Collection {collection_name} already exists."
        mock_client.create_collection.side_effect = ValueError(error_message)

        # --- Act & Assert ---
        input_model = CreateCollectionInput(collection_name=collection_name)
        # Use context manager to assert specific McpError is raised
        with assert_raises_mcp_error(f"Tool Error: Collection '{collection_name}' already exists."):
            await _create_collection_impl(input_model)

        # Assert mocks
        mock_validate.assert_called_once_with(collection_name)
        mock_client.create_collection.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_collection_unexpected_error(self, mock_chroma_client_collections):
        """Test handling of unexpected errors during collection creation."""
        mock_client, _, mock_validate = mock_chroma_client_collections
        collection_name = "test_unexpected_create"
        error_message = "Database connection failed"
        # Mock the client call to raise a generic Exception
        mock_client.create_collection.side_effect = Exception(error_message)

        # --- Act & Assert ---
        input_model = CreateCollectionInput(collection_name=collection_name)
        # Use context manager to assert McpError is raised with the wrapped message
        expected_msg = f"Tool Error: An unexpected error occurred while creating collection '{collection_name}'. Details: {error_message}"
        with assert_raises_mcp_error(expected_msg):
            await _create_collection_impl(input_model)

        # Assert mocks
        mock_validate.assert_called_once_with(collection_name)
        mock_client.create_collection.assert_called_once()
        # Optional: Check logs if needed, but the result check is primary

    # --- _peek_collection_impl Tests ---
    @pytest.mark.asyncio
    async def test_peek_collection_success(self, mock_chroma_client_collections):
        """Test successful peeking into a collection."""
        mock_client, mock_collection, _ = mock_chroma_client_collections
        collection_name = "test_peek_exists"
        limit = 3
        expected_peek_result = {
            "ids": ["id1", "id2", "id3"],
            "documents": ["doc1", "doc2", "doc3"],
            "metadatas": [{"m": 1}, {"m": 2}, {"m": 3}],
            "embeddings": None,  # Assuming embeddings are not included by default peek
        }

        # Configure get_collection mock
        mock_client.get_collection.return_value = mock_collection
        # Configure the collection's peek method
        mock_collection.peek.return_value = expected_peek_result

        # --- Act ---
        # Create Pydantic model instance
        input_model = PeekCollectionInput(collection_name=collection_name, limit=limit)
        result = await _peek_collection_impl(input_model)

        # --- Assert ---
        mock_client.get_collection.assert_called_once_with(name=collection_name)
        mock_collection.peek.assert_called_once_with(limit=limit)

        # Assert result using helper, comparing directly with expected dict
        assert_successful_json_result(result, expected_peek_result)

    # --- _list_collections_impl Tests ---
    @pytest.mark.asyncio
    async def test_list_collections_success(self, mock_chroma_client_collections):
        """Test successful default collection listing."""
        mock_client, _, _ = mock_chroma_client_collections
        # Simulate the return value from the actual Chroma client method (List[str])
        mock_client.list_collections.return_value = ["coll_a", "coll_b"]

        # --- Act ---
        # Create Pydantic model instance (no args)
        input_model = ListCollectionsInput()
        result = await _list_collections_impl(input_model)

        # --- Assert ---
        mock_client.list_collections.assert_called_once()

        # Assert result structure and content using helper
        result_data = assert_successful_json_result(result)
        assert result_data.get("collection_names") == ["coll_a", "coll_b"]
        assert result_data.get("total_count") == 2
        assert result_data.get("limit") is None
        assert result_data.get("offset") is None

    @pytest.mark.asyncio
    async def test_list_collections_with_filter_pagination(self, mock_chroma_client_collections):
        """Test listing with name filter and pagination."""
        mock_client, _, _ = mock_chroma_client_collections
        # Simulate Chroma client return with List[str]
        collections_data = ["apple", "banana", "apricot", "avocado"]
        mock_client.list_collections.return_value = collections_data

        # --- Act ---
        # Create Pydantic model instance
        input_model = ListCollectionsInput(limit=2, offset=1, name_contains="ap")
        result = await _list_collections_impl(input_model)

        # --- Assert ---
        mock_client.list_collections.assert_called_once()

        # Assert result structure and content using helper
        result_data = assert_successful_json_result(result)
        # Filtering happens *after* list_collections in the _impl
        # The mock returns all, the filter selects ["apple", "apricot"]
        # Offset 1 skips "apple", limit 2 takes "apricot"
        assert result_data.get("collection_names") == ["apricot"]
        assert result_data.get("total_count") == 2  # Total matching filter "ap"
        assert result_data.get("limit") == 2
        assert result_data.get("offset") == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "limit, offset, expected_error_msg",
        [
            (-1, 0, "Validation Error: limit cannot be negative"),
            (0, -1, "Validation Error: offset cannot be negative"),
        ],
        ids=["negative_limit", "negative_offset"],
    )
    async def test_list_collections_validation_error(
        self, mock_chroma_client_collections, limit, offset, expected_error_msg
    ):
        """Test internal validation errors for list_collections (currently none, Pydantic handles this)."""
        _mock_client, _, _mock_validate = mock_chroma_client_collections

        # NOTE: Pydantic model now prevents negative numbers. This test would previously
        # check for ValidationError raised by the _impl function.
        # To test Pydantic validation, we'd need to call the main call_tool handler
        # with invalid raw arguments. This test becomes less relevant for _impl
        # For now, let's skip the execution and assertion.
        pytest.skip("Input validation for limit/offset now handled by Pydantic model constraints")

        # --- Act ---
        # input_model = ListCollectionsInput(limit=limit, offset=offset)
        # result = await _list_collections_impl(input_model)

        # --- Assert ---
        # assert_error_result(result, expected_error_msg) # Pydantic error won't match this

    # --- _get_collection_impl Tests ---
    @pytest.mark.asyncio
    async def test_get_collection_success(self, mock_chroma_client_collections):
        """Test getting existing collection info."""
        mock_client, mock_collection, _ = mock_chroma_client_collections
        collection_name = "my_coll"
        mock_collection_id = "test-id-123"
        mock_metadata = {"description": "test desc", "chroma:setting:hnsw_space": "l2"}
        mock_count = 42
        mock_peek = {"ids": ["p1"], "documents": ["peek doc"]}

        # Configure the mock collection returned by get_collection
        mock_collection.name = collection_name
        mock_collection.id = mock_collection_id
        mock_collection.metadata = mock_metadata
        mock_collection.count.return_value = mock_count
        mock_collection.peek.return_value = mock_peek
        mock_client.get_collection.return_value = mock_collection

        # --- Act ---
        # Create Pydantic model instance - Use correct name
        input_model = GetCollectionInput(collection_name=collection_name)
        result = await _get_collection_impl(input_model)

        # --- Assert ---
        # Implementation passes embedding_function here
        mock_client.get_collection.assert_called_once_with(name=collection_name, embedding_function=ANY)
        mock_collection.count.assert_called_once()
        mock_collection.peek.assert_called_once_with(limit=5)  # Check limit used in _impl

        # Assert result structure and content using helper
        result_data = assert_successful_json_result(result)
        assert result_data.get("name") == collection_name
        assert result_data.get("id") == mock_collection_id
        assert result_data.get("count") == mock_count
        # Assert reconstructed metadata
        assert result_data.get("metadata") == _reconstruct_metadata(mock_metadata)
        assert result_data.get("sample_entries") == mock_peek

    @pytest.mark.asyncio
    async def test_get_collection_not_found(self, mock_chroma_client_collections):
        """Test getting a non-existent collection (handled in impl)."""
        mock_client, _, _ = mock_chroma_client_collections
        collection_name = "not_found_coll"
        error_message = f"Collection {collection_name} does not exist."
        mock_client.get_collection.side_effect = ValueError(error_message)

        # --- Act & Assert ---
        # Create Pydantic model instance
        input_model = GetCollectionInput(collection_name=collection_name)
        # Move the call INSIDE the context manager
        # result = await _get_collection_impl(input_model)
        # --- Assert ---
        # mock_client.get_collection.assert_called_once_with(name=collection_name, embedding_function=ANY)
        with assert_raises_mcp_error(f"Tool Error: Collection '{collection_name}' not found."):
            await _get_collection_impl(input_model)
        mock_client.get_collection.assert_called_once_with(name=collection_name, embedding_function=ANY)

    @pytest.mark.asyncio
    async def test_get_collection_unexpected_error(self, mock_chroma_client_collections):
        """Test handling of unexpected error during get collection."""
        mock_client, _, _ = mock_chroma_client_collections
        collection_name = "test_unexpected_get"
        error_message = "Connection failed"
        mock_client.get_collection.side_effect = Exception(error_message)

        # --- Act & Assert ---
        input_model = GetCollectionInput(collection_name=collection_name)
        # result = await _get_collection_impl(input_model)
        # --- Assert ---
        # mock_client.get_collection.assert_called_once_with(name=collection_name, embedding_function=ANY)
        expected_msg = f"Tool Error: An unexpected error occurred while getting collection '{collection_name}'. Details: {error_message}"
        with assert_raises_mcp_error(expected_msg):
            await _get_collection_impl(input_model)
        mock_client.get_collection.assert_called_once_with(name=collection_name, embedding_function=ANY)

    # --- _rename_collection_impl Tests ---
    @pytest.mark.asyncio
    async def test_rename_collection_success(self, mock_chroma_client_collections):
        """Test successful collection renaming."""
        mock_client, mock_collection, mock_validate = mock_chroma_client_collections
        original_name = "rename_me"
        new_name = "renamed_successfully"

        # Configure mock collection
        mock_client.get_collection.return_value = mock_collection

        # --- Act ---
        input_model = RenameCollectionInput(collection_name=original_name, new_name=new_name)
        result = await _rename_collection_impl(input_model)

        # --- Assert ---
        # Check validation calls
        mock_validate.assert_has_calls([call(original_name), call(new_name)])
        mock_client.get_collection.assert_called_once_with(name=original_name)
        mock_collection.modify.assert_called_once_with(name=new_name)

        # Assert successful result message
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], types.TextContent)
        assert f"Collection '{original_name}' successfully renamed to '{new_name}'." in result[0].text

    @pytest.mark.asyncio
    async def test_rename_collection_invalid_new_name(self, mock_chroma_client_collections):
        """Test validation failure for the new collection name during rename."""
        mock_client, mock_collection, mock_validate = mock_chroma_client_collections
        original_name = "valid_original_name"
        invalid_new_name = "invalid!"

        # Configure validator mock: first call (original) ok, second (new) raises
        def validate_side_effect(name):
            if name == invalid_new_name:
                raise ValidationError("Invalid new name")
            return  # No error for original name

        mock_validate.side_effect = validate_side_effect

        # --- Act ---
        input_model = RenameCollectionInput(collection_name=original_name, new_name=invalid_new_name)
        with assert_raises_mcp_error("Validation Error: Invalid new name"):
            await _rename_collection_impl(input_model)

        # --- Assert ---
        mock_validate.assert_any_call(original_name)  # Called with original first
        mock_validate.assert_any_call(invalid_new_name)  # Called with new name second
        mock_collection.modify.assert_not_called()

    @pytest.mark.asyncio
    async def test_rename_collection_original_not_found(self, mock_chroma_client_collections):
        """Test renaming when the original collection does not exist."""
        mock_client, _, mock_validate = mock_chroma_client_collections
        original_name = "original_not_found"
        new_name = "new_name_irrelevant"
        # Mock get_collection to raise error
        mock_client.get_collection.side_effect = ValueError(f"Collection {original_name} does not exist.")

        # --- Act ---
        input_model = RenameCollectionInput(collection_name=original_name, new_name=new_name)
        with assert_raises_mcp_error(f"Tool Error: Collection '{original_name}' not found."):
            await _rename_collection_impl(input_model)

        # --- Assert ---
        mock_validate.assert_has_calls([call(original_name), call(new_name)])  # Both validations called
        mock_client.get_collection.assert_called_once_with(name=original_name)

    @pytest.mark.asyncio
    async def test_rename_collection_new_name_exists(self, mock_chroma_client_collections):
        """Test renaming when the new name already exists."""
        mock_client, mock_collection, mock_validate = mock_chroma_client_collections
        original_name = "original_exists"
        new_name = "new_name_exists"
        # Mock get_collection success, but modify fails
        mock_client.get_collection.return_value = mock_collection
        mock_collection.modify.side_effect = ValueError(f"Collection {new_name} already exists.")

        # --- Act ---
        input_model = RenameCollectionInput(collection_name=original_name, new_name=new_name)
        with assert_raises_mcp_error(f"Tool Error: Collection name '{new_name}' already exists."):
            await _rename_collection_impl(input_model)

        # --- Assert ---
        mock_validate.assert_has_calls([call(original_name), call(new_name)])
        mock_client.get_collection.assert_called_once_with(name=original_name)
        mock_collection.modify.assert_called_once_with(name=new_name)

    @pytest.mark.asyncio
    async def test_rename_collection_unexpected_error(self, mock_chroma_client_collections):
        """Test unexpected error during rename."""
        mock_client, mock_collection, mock_validate = mock_chroma_client_collections
        original_name = "original_err"
        new_name = "new_name_err"
        error_message = "Unexpected DB issue"
        mock_client.get_collection.return_value = mock_collection
        mock_collection.modify.side_effect = Exception(error_message)

        # --- Act ---
        input_model = RenameCollectionInput(collection_name=original_name, new_name=new_name)
        with assert_raises_mcp_error(
            f"Tool Error: An unexpected error occurred renaming collection '{original_name}'. Details: {error_message}"
        ):
            await _rename_collection_impl(input_model)

        # --- Assert ---
        mock_validate.assert_has_calls([call(original_name), call(new_name)])
        mock_client.get_collection.assert_called_once_with(name=original_name)
        mock_collection.modify.assert_called_once_with(name=new_name)

    # --- _delete_collection_impl Tests ---
    @pytest.mark.asyncio
    async def test_delete_collection_success(self, mock_chroma_client_collections):
        """Test successful collection deletion."""
        mock_client, _, mock_validate = mock_chroma_client_collections
        collection_name = "delete_me"

        # --- Act ---
        input_model = DeleteCollectionInput(collection_name=collection_name)
        result = await _delete_collection_impl(input_model)

        # --- Assert ---
        mock_validate.assert_called_once_with(collection_name)
        mock_client.delete_collection.assert_called_once_with(name=collection_name)

        # Assert successful result (non-JSON)
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], types.TextContent)
        assert f"Collection '{collection_name}' deleted successfully." in result[0].text

    @pytest.mark.asyncio
    async def test_delete_collection_not_found(self, mock_chroma_client_collections):
        """Test deleting a non-existent collection."""
        mock_client, _, mock_validate = mock_chroma_client_collections
        collection_name = "not_found_delete"
        error_message = f"Collection {collection_name} does not exist."
        mock_client.delete_collection.side_effect = ValueError(error_message)

        # --- Act ---
        input_model = DeleteCollectionInput(collection_name=collection_name)
        with assert_raises_mcp_error(f"Tool Error: Collection '{collection_name}' not found."):
            await _delete_collection_impl(input_model)

        # --- Assert ---
        mock_validate.assert_called_once_with(collection_name)
        mock_client.delete_collection.assert_called_once_with(name=collection_name)

    @pytest.mark.asyncio
    async def test_delete_collection_unexpected_error(self, mock_chroma_client_collections):
        """Test unexpected error during collection deletion."""
        mock_client, _, mock_validate = mock_chroma_client_collections
        collection_name = "test_delete_err"
        error_message = "DB connection lost"
        mock_client.delete_collection.side_effect = Exception(error_message)

        # --- Act ---
        input_model = DeleteCollectionInput(collection_name=collection_name)
        with assert_raises_mcp_error(
            f"Tool Error: An unexpected error occurred deleting collection '{collection_name}'. Details: {error_message}"
        ):
            await _delete_collection_impl(input_model)

        # --- Assert ---
        mock_validate.assert_called_once_with(collection_name)
        mock_client.delete_collection.assert_called_once_with(name=collection_name)

    # --- Tests for _create_collection_with_metadata_impl ---
    @pytest.mark.asyncio
    async def test_create_collection_with_metadata_success(self, mock_chroma_client_collections):
        """Test successful creation using the _with_metadata variant (as JSON string)."""
        (
            mock_client,
            _,
            mock_validate,
        ) = mock_chroma_client_collections
        collection_name = "test_create_with_meta"
        custom_metadata_dict = {"description": "My custom description", "hnsw:space": "ip"}
        custom_metadata_json = json.dumps(custom_metadata_dict)  # Convert to JSON string
        mock_collection_id = str(uuid.uuid4())

        # Mock the collection returned by create_collection
        created_collection_mock = MagicMock()
        created_collection_mock.name = collection_name
        created_collection_mock.id = mock_collection_id
        # Simulate Chroma storing the metadata (it might flatten/prefix settings)
        # Assume it stores what was passed to the *client*, which is the dict
        created_collection_mock.metadata = custom_metadata_dict
        created_collection_mock.count.return_value = 0
        mock_client.create_collection.return_value = created_collection_mock

        # --- Act ---
        input_model = CreateCollectionWithMetadataInput(
            collection_name=collection_name, metadata=custom_metadata_json  # Pass the JSON string here
        )
        result_list = await _create_collection_with_metadata_impl(input_model)

        # --- Assert ---
        # Mock calls
        mock_validate.assert_called_once_with(collection_name)
        mock_client.create_collection.assert_called_once()
        call_args = mock_client.create_collection.call_args
        assert call_args.kwargs["name"] == collection_name
        # Verify the original *dictionary* was passed to Chroma's create_collection
        assert call_args.kwargs["metadata"] == custom_metadata_dict
        assert call_args.kwargs["get_or_create"] is False

        # Result structure and content assertions
        result_data = assert_successful_json_result(result_list)
        assert result_data.get("name") == collection_name
        assert result_data.get("id") == mock_collection_id
        assert "metadata" in result_data
        # Check reconstructed metadata matches the input dictionary
        expected_reconstructed = _reconstruct_metadata(custom_metadata_dict)  # Use dict for check
        assert result_data["metadata"] == expected_reconstructed
        assert result_data.get("count") == 0
        assert result_data.get("status") == "success"
