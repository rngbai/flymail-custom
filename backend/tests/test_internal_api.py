import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class _AppError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _load_internal_route_module(downloads_dir: Path):
    fastapi_stub = types.ModuleType("fastapi")

    class _Router:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            return lambda func: func

    fastapi_stub.APIRouter = _Router
    fastapi_stub.Query = lambda default=None, *args, **kwargs: default
    fastapi_stub.Request = object

    responses_stub = types.ModuleType("fastapi.responses")
    responses_stub.Response = object

    data_paths_stub = types.ModuleType("data_paths")
    data_paths_stub.DOWNLOADS_DIR = downloads_dir

    db_stub = types.ModuleType("db")
    for name in (
        "get_account_by_id",
        "get_accounts",
        "get_cached_message_detail",
        "get_cached_messages_by_folder",
        "list_cached_attachments",
        "search_cached_messages_by_folder",
    ):
        setattr(db_stub, name, AsyncMock())

    errors_stub = types.ModuleType("errors")
    errors_stub.AppError = _AppError

    factory_stub = types.ModuleType("providers.factory")
    factory_stub.ProviderFactory = object()

    token_stub = types.ModuleType("services.token")
    token_stub.ensure_token = AsyncMock()

    logger_stub = types.ModuleType("utils.logger")
    logger_stub.get_logger = lambda _name: types.SimpleNamespace(
        warning=lambda *args, **kwargs: None
    )

    modules = {
        "fastapi": fastapi_stub,
        "fastapi.responses": responses_stub,
        "data_paths": data_paths_stub,
        "db": db_stub,
        "errors": errors_stub,
        "providers.factory": factory_stub,
        "services.token": token_stub,
        "utils.logger": logger_stub,
    }
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        module_path = Path(__file__).resolve().parents[1] / "routes" / "internal.py"
        spec = importlib.util.spec_from_file_location("internal_route_for_test", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, db_stub
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class InternalApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.downloads_dir = Path(self.temp_dir.name) / "download"
        self.downloads_dir.mkdir()
        self.internal, self.db = _load_internal_route_module(self.downloads_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_service_token_is_required_and_compared(self):
        request = types.SimpleNamespace(headers={})
        with patch.dict(os.environ, {"FLYMAIL_SERVICE_TOKEN": "secret"}, clear=False):
            with self.assertRaises(_AppError) as raised:
                self.internal._require_token(request)
            self.assertEqual(raised.exception.status_code, 401)

            request.headers["X-FlyMail-Service-Token"] = "secret"
            self.internal._require_token(request)

    def test_attachment_path_cannot_escape_download_directory(self):
        safe_file = self.downloads_dir / "account" / "file.pdf"
        safe_file.parent.mkdir()
        safe_file.write_bytes(b"pdf")
        self.assertEqual(self.internal._safe_local_path(str(safe_file)), safe_file.resolve())

        with self.assertRaises(_AppError) as raised:
            self.internal._safe_local_path(str(Path(self.temp_dir.name) / "outside.pdf"))
        self.assertEqual(raised.exception.status_code, 403)

    async def test_message_search_uses_cache_and_caps_page_size(self):
        account = types.SimpleNamespace(id="account-1", user_uid="user-1")
        self.db.get_account_by_id.return_value = account
        self.db.search_cached_messages_by_folder.return_value = {
            "messages": [],
            "total": 0,
        }
        request = types.SimpleNamespace(
            headers={"X-FlyMail-Service-Token": "secret"},
            query_params={},
        )

        with patch.dict(os.environ, {"FLYMAIL_SERVICE_TOKEN": "secret"}, clear=False):
            result = await self.internal.internal_messages(
                request,
                account_id="account-1",
                folder="INBOX",
                query="invoice",
                page=0,
                page_size=999,
                read_filter="unread",
                attachment_only=True,
            )

        self.db.search_cached_messages_by_folder.assert_awaited_once_with(
            "user-1",
            "account-1",
            "INBOX",
            "invoice",
            page=1,
            page_size=200,
            read_filter="unread",
            attachment_filter=True,
        )
        self.assertEqual(result["account_id"], "account-1")
        self.assertEqual(result["query"], "invoice")


if __name__ == "__main__":
    unittest.main()
