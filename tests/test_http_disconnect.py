"""媒体 Range 取消和部分响应失败不能触发重复回包或错误日志刷屏。"""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from naiba.http import RequestHandler


class HttpDisconnectTests(unittest.TestCase):
    def handler(self):
        handler = RequestHandler.__new__(RequestHandler)
        handler.rfile = io.BytesIO(b"GET /probe HTTP/1.1\r\n")
        handler.wfile = io.BytesIO()
        handler.command = "GET"
        handler.request_version = "HTTP/1.1"
        handler._headers_buffer = []
        handler.parse_request = Mock(return_value=True)
        handler.log_error = Mock()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.close_connection = False
        return handler

    def test_cancelled_requests_do_not_log_or_send_another_response(self):
        for error_type in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            with self.subTest(error=error_type):
                handler = self.handler()
                handler.do_GET = Mock(side_effect=error_type("媒体请求已取消"))
                handler.handle_one_request()
                self.assertTrue(handler.close_connection)
                handler.log_error.assert_not_called()
                handler.send_response.assert_not_called()

    def test_failure_after_headers_keeps_single_response(self):
        handler = self.handler()

        def partial_response():
            handler._headers_buffer = [b"HTTP/1.0 206 Partial Content\r\n"]
            handler.end_headers()
            raise RuntimeError("发送途中失败")

        handler.do_GET = partial_response
        handler.handle_one_request()
        self.assertTrue(handler.headers_sent)
        handler.send_response.assert_not_called()
        self.assertNotIn(b"500", handler.wfile.getvalue())
        handler.log_error.assert_called_once()

    def test_failure_before_headers_still_reports_500(self):
        handler = self.handler()
        # 同一 keep-alive handler 的上一份响应标记必须在新请求开始时清掉。
        handler.headers_sent = True
        handler.do_GET = Mock(side_effect=RuntimeError("业务失败"))
        handler.handle_one_request()
        handler.send_response.assert_called_once_with(500)
        self.assertIn("服务器内部错误".encode(), handler.wfile.getvalue())

    def test_file_reset_is_quiet_but_disk_errors_remain_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "clip.mp4"
            video.write_bytes(b"video bytes")
            for error in (ConnectionResetError("已取消"), OSError("磁盘读取失败")):
                with self.subTest(error=type(error)):
                    handler = self.handler()
                    handler.server = SimpleNamespace(app=SimpleNamespace(
                        paths=SimpleNamespace(data_dir=root),
                        config=SimpleNamespace(resolve_workspace_dir=lambda: root),
                    ))
                    handler.headers = {"Range": "bytes=0-4"}
                    handler.wfile = Mock()
                    handler.wfile.write.side_effect = error
                    handler.end_headers = Mock()
                    with patch("naiba.http.logger.warning") as warning:
                        handler._serve_local_file(str(video))
                    self.assertTrue(handler.close_connection)
                    if isinstance(error, ConnectionResetError):
                        warning.assert_not_called()
                    else:
                        warning.assert_called_once()
                    handler.send_response.assert_called_once_with(206)


if __name__ == "__main__":
    unittest.main()
