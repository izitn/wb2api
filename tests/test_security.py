#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全相关的回归测试：日志脱敏、敏感数据保护"""

import sys
import unittest
from pathlib import Path

# 将项目根目录添加到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

import wb2api as wb


class LogSanitizationTests(unittest.TestCase):
    """日志脱敏测试 - 确保敏感信息不会泄露到日志"""

    def test_no_access_token_in_sanitized_text(self):
        """accessToken 应被脱敏"""
        raw = '{"error": {"accessToken": "secret-token-12345"}}'
        sanitized = wb._sanitize_for_log(raw)
        self.assertNotIn("secret-token-12345", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_no_device_token_in_sanitized_text(self):
        """deviceToken 应被脱敏"""
        raw = '{"deviceToken": "device-secret-abc123"}'
        sanitized = wb._sanitize_for_log(raw)
        self.assertNotIn("device-secret-abc123", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_no_refresh_token_in_sanitized_text(self):
        """refreshToken 应被脱敏"""
        raw = '{"refreshToken": "refresh-secret-xyz789"}'
        sanitized = wb._sanitize_for_log(raw)
        self.assertNotIn("refresh-secret-xyz789", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_no_api_key_in_sanitized_text(self):
        """本地 API key 应被脱敏"""
        raw = 'Authorization failed for key sk-0123456789abcdef0123456789abcdef'
        sanitized = wb._sanitize_for_log(raw)
        self.assertNotIn("0123456789abcdef0123456789abcdef", sanitized)
        self.assertIn("sk-[REDACTED]", sanitized)

    def test_no_bearer_token_in_sanitized_text(self):
        """Bearer token 应被脱敏"""
        raw = 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test.token'
        sanitized = wb._sanitize_for_log(raw)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", sanitized)
        self.assertIn("Bearer [REDACTED]", sanitized)

    def test_no_newline_injection_in_log(self):
        """换行符应被压平，防止日志注入"""
        raw = 'First line\nFAKE LOG ENTRY\rAnother fake line'
        sanitized = wb._sanitize_for_log(raw)
        self.assertNotIn('\n', sanitized)
        self.assertNotIn('\r', sanitized)
        # 应该被替换为空格
        self.assertIn('First line', sanitized)
        self.assertIn('FAKE LOG ENTRY', sanitized)

    def test_long_user_messages_are_truncated(self):
        """长的用户消息内容应被截断"""
        raw = '{"messages": [{"content": "' + 'x' * 500 + '"}]}'
        sanitized = wb._sanitize_for_log(raw, max_len=200)
        self.assertLess(len(sanitized), 250)  # 200 + [TRUNCATED] 等标记
        self.assertIn("[REDACTED]", sanitized)  # messages 内容应被脱敏

    def test_long_content_is_truncated(self):
        """长的 content 字段应被截断"""
        raw = '{"content": "' + 'y' * 200 + '"}'
        sanitized = wb._sanitize_for_log(raw, max_len=150)
        self.assertLess(len(sanitized), 200)
        self.assertIn("[TRUNCATED]", sanitized)

    def test_empty_input_returns_empty(self):
        """空输入应返回空字符串"""
        self.assertEqual(wb._sanitize_for_log(""), "")
        self.assertEqual(wb._sanitize_for_log(None), "")

    def test_normal_error_message_is_preserved(self):
        """正常的错误消息应保留关键信息"""
        raw = '{"code": 429, "message": "Rate limit exceeded"}'
        sanitized = wb._sanitize_for_log(raw)
        self.assertIn("429", sanitized)
        self.assertIn("Rate limit", sanitized)

    def test_combined_sensitive_data(self):
        """多种敏感数据应同时被脱敏"""
        raw = '''{"error": "Auth failed", "accessToken": "secret1",
                  "deviceToken": "secret2", "auth": "Bearer secret3xyz"}'''
        sanitized = wb._sanitize_for_log(raw)
        self.assertNotIn("secret1", sanitized)
        self.assertNotIn("secret2", sanitized)
        self.assertNotIn("secret3xyz", sanitized)
        # 至少有一个 REDACTED
        self.assertIn("[REDACTED]", sanitized)


class KeyMaskingTests(unittest.TestCase):
    """API key 掩码测试 - 确保启动日志中的密钥被掩码"""

    def test_long_key_is_masked(self):
        """长密钥应显示前缀和后缀"""
        key = "sk-0123456789abcdef0123456789abcdef"
        masked = wb._mask_api_key(key)
        self.assertEqual(masked, "sk-0123...cdef")
        self.assertNotIn("0123456789abcdef012345678", masked)

    def test_short_key_fully_masked(self):
        """短密钥应完全掩码"""
        key = "sk-short"
        masked = wb._mask_api_key(key)
        self.assertEqual(masked, "***")

    def test_empty_key_masked(self):
        """空密钥应返回占位符"""
        self.assertEqual(wb._mask_api_key(""), "***")
        self.assertEqual(wb._mask_api_key(None), "***")

    def test_boundary_length_15(self):
        """长度为 15 的密钥是边界情况"""
        key = "sk-123456789abc"  # 恰好 15 字符
        masked = wb._mask_api_key(key)
        self.assertEqual(masked, "***")

    def test_boundary_length_16(self):
        """长度为 16 的密钥应显示掩码"""
        key = "sk-123456789abcd"  # 16 字符
        masked = wb._mask_api_key(key)
        self.assertEqual(masked, "sk-1234...abcd")


if __name__ == "__main__":
    unittest.main()
