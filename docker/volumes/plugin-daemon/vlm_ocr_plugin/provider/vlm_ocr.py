from dify_plugin import ToolProvider


class VlmOcrProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict) -> bool:
        """No-op validation; credentials are consumed by the tool runtime."""
        return True
