# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from naiba.config import ConfigStore


class KVMemConfigTests(unittest.TestCase):
    def test_kvmem_fields_roundtrip_and_public_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            saved = store.upsert_model_profile({
                'id': 'kv', 'kind': 'local', 'name': 'KV',
                'base_url': 'http://127.0.0.1:18200/v1', 'model': 'm',
                'request_format': 'llama_cpp', 'local_backend': 'llama_cpp',
                'service_manager': 'kvmem', 'service_manager_url': 'http://127.0.0.1:18199',
            })
            self.assertEqual(saved['service_manager'], 'kvmem')
            self.assertEqual(store.model_profiles('local')[0]['service_manager_url'], 'http://127.0.0.1:18199')

    def test_remote_manager_url_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            with self.assertRaises(ValueError):
                store.upsert_model_profile({
                    'id': 'kv', 'kind': 'local', 'name': 'KV',
                    'base_url': 'http://127.0.0.1:18200/v1', 'model': 'm',
                    'request_format': 'llama_cpp', 'local_backend': 'llama_cpp',
                    'service_manager': 'kvmem', 'service_manager_url': 'http://192.168.1.5:18199',
                })

    def test_plain_llama_cpp_has_no_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'config.json')
            saved = store.upsert_model_profile({
                'id': 'llama', 'kind': 'local', 'name': 'Llama',
                'base_url': 'http://127.0.0.1:8080/v1', 'model': 'm',
                'request_format': 'llama_cpp', 'local_backend': 'llama_cpp',
            })
            self.assertNotIn('service_manager', saved)


if __name__ == '__main__':
    unittest.main()
