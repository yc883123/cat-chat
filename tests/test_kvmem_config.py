# -*- coding: utf-8 -*-
"""KVMem 供应商配置存储测试：完整启动配置的持久化、脱敏与兼容。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from naiba.config import ConfigStore


class KVMemConfigTests(unittest.TestCase):
    def _store(self, directory):
        return ConfigStore(Path(directory) / 'config.json')

    def test_kvmem_fields_roundtrip_and_public_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
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
            store = self._store(directory)
            with self.assertRaises(ValueError):
                store.upsert_model_profile({
                    'id': 'kv', 'kind': 'local', 'name': 'KV',
                    'base_url': 'http://127.0.0.1:18200/v1', 'model': 'm',
                    'request_format': 'llama_cpp', 'local_backend': 'llama_cpp',
                    'service_manager': 'kvmem', 'service_manager_url': 'http://192.168.1.5:18199',
                })

    def test_plain_llama_cpp_has_no_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            saved = store.upsert_model_profile({
                'id': 'llama', 'kind': 'local', 'name': 'Llama',
                'base_url': 'http://127.0.0.1:8080/v1', 'model': 'm',
                'request_format': 'llama_cpp', 'local_backend': 'llama_cpp',
            })
            self.assertNotIn('service_manager', saved)

    def test_install_dir_and_launch_config_roundtrip_with_types(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            saved = store.upsert_model_profile({
                'id': 'kv', 'kind': 'local', 'name': 'KV',
                'base_url': 'http://127.0.0.1:18200/v1', 'model': 'm',
                'request_format': 'llama_cpp', 'local_backend': 'llama_cpp',
                'service_manager': 'kvmem', 'service_manager_url': 'http://127.0.0.1:18199',
                'kvmem_install_dir': 'D:/kvmem-v0.19.1-windows-x64-nvidia-50-unified',
                'kvmem_launch_config': {'schema_version': 1, 'parameters': {
                    'Model': r'D:\models\qwen.gguf', 'Backend': 'ninfer', 'Port': 18200,
                    'Context': 262144, 'FastPrefill': True, 'GraphAlpha': 0.2,
                    'WorkerArgs': ['--threads', '8'],
                }},
            })
            self.assertEqual(saved['kvmem_install_dir'], 'D:\\kvmem-v0.19.1-windows-x64-nvidia-50-unified')
            parameters = saved['kvmem_launch_config']['parameters']
            self.assertEqual(parameters['Context'], 262144)
            self.assertIs(parameters['FastPrefill'], True)
            self.assertEqual(parameters['GraphAlpha'], 0.2)
            self.assertEqual(parameters['WorkerArgs'], ['--threads', '8'])
            # model_profiles 保留完整参数（无密钥，可安全回显表单）。
            stored = store.model_profiles('local')[0]
            self.assertEqual(stored['kvmem_launch_config']['parameters']['Model'], r'D:\models\qwen.gguf')

    def test_api_key_never_lands_in_launch_config(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            saved = store.upsert_model_profile({
                'id': 'kv', 'kind': 'local', 'name': 'KV',
                'base_url': 'http://127.0.0.1:18200/v1', 'model': 'm', 'api_key': 'sk-provider-1',
                'request_format': 'llama_cpp', 'local_backend': 'llama_cpp',
                'service_manager': 'kvmem', 'service_manager_url': 'http://127.0.0.1:18199',
                'kvmem_launch_config': {'schema_version': 1, 'parameters': {
                    'Model': 'm.gguf', 'Backend': 'ninfer', 'ApiKey': 'sk-inside',
                }},
            })
            self.assertNotIn('ApiKey', saved['kvmem_launch_config']['parameters'])
            raw = (Path(directory) / 'config.json').read_text(encoding='utf-8')
            self.assertNotIn('sk-inside', raw)
            self.assertIn('sk-provider-1', raw)  # 供应商密钥走原有密钥存储。

    def test_invalid_launch_config_is_rejected(self):
        cases = [
            {'schema_version': 2, 'parameters': {}},
            {'schema_version': 1, 'parameters': 'nope'},
            {'schema_version': 1, 'parameters': {'Model': {'bad': 'type'}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            for config in cases:
                with self.subTest(config=config):
                    with self.assertRaises(ValueError):
                        store.upsert_model_profile({
                            'id': 'kv', 'kind': 'local', 'name': 'KV',
                            'base_url': 'http://127.0.0.1:18200/v1', 'model': 'm',
                            'request_format': 'llama_cpp', 'local_backend': 'llama_cpp',
                            'service_manager': 'kvmem', 'service_manager_url': 'http://127.0.0.1:18199',
                            'kvmem_launch_config': config,
                        })

    def test_relative_install_dir_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            with self.assertRaises(ValueError):
                store.upsert_model_profile({
                    'id': 'kv', 'kind': 'local', 'name': 'KV',
                    'base_url': 'http://127.0.0.1:18200/v1', 'model': 'm',
                    'request_format': 'llama_cpp', 'local_backend': 'llama_cpp',
                    'service_manager': 'kvmem', 'service_manager_url': 'http://127.0.0.1:18199',
                    'kvmem_install_dir': 'relative/path',
                })

    def test_absent_kvmem_keys_preserve_existing_config(self):
        """旧路径/旧客户端编辑卡片不带 KVMem 键时，不能静默清掉启动配置。"""
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            store.upsert_model_profile({
                'id': 'kv', 'kind': 'local', 'name': 'KV',
                'base_url': 'http://127.0.0.1:18200/v1', 'model': 'm',
                'request_format': 'llama_cpp', 'local_backend': 'llama_cpp',
                'service_manager': 'kvmem', 'service_manager_url': 'http://127.0.0.1:18199',
                'kvmem_install_dir': 'D:/kvmem',
                'kvmem_launch_config': {'schema_version': 1, 'parameters': {'Model': 'm.gguf', 'Backend': 'ninfer'}},
            })
            store.upsert_model_profile({
                'id': 'kv', 'kind': 'local', 'name': 'KV-renamed',
                'base_url': 'http://127.0.0.1:18200/v1', 'model': 'm',
                'request_format': 'llama_cpp', 'local_backend': 'llama_cpp',
            })
            stored = store.model_profiles('local')[0]
            self.assertEqual(stored['kvmem_install_dir'], 'D:\\kvmem')
            self.assertEqual(stored['kvmem_launch_config']['parameters']['Model'], 'm.gguf')

    def test_explicit_empty_launch_config_clears_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            store.upsert_model_profile({
                'id': 'kv', 'kind': 'local', 'name': 'KV',
                'base_url': 'http://127.0.0.1:18200/v1', 'model': 'm',
                'request_format': 'llama_cpp', 'local_backend': 'llama_cpp',
                'service_manager': 'kvmem', 'service_manager_url': 'http://127.0.0.1:18199',
                'kvmem_install_dir': 'D:/kvmem',
                'kvmem_launch_config': {'schema_version': 1, 'parameters': {'Model': 'm.gguf', 'Backend': 'ninfer'}},
            })
            store.upsert_model_profile({
                'id': 'kv', 'kind': 'local', 'name': 'KV',
                'base_url': 'http://127.0.0.1:18200/v1', 'model': 'm',
                'request_format': 'llama_cpp', 'local_backend': 'llama_cpp',
                'service_manager': 'kvmem', 'service_manager_url': 'http://127.0.0.1:18199',
                'kvmem_install_dir': '',
                'kvmem_launch_config': None,
            })
            stored = store.model_profiles('local')[0]
            self.assertEqual(stored.get('kvmem_install_dir'), '')
            self.assertNotIn('kvmem_launch_config', stored)


if __name__ == '__main__':
    unittest.main()
